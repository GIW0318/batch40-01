import base64
import hashlib
from collections.abc import Mapping


SECURE = "secure"
INSECURE = "insecure"
BOGUS = "bogus"


def _as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _name(value):
    if value is None:
        return ""
    name = str(value).strip().lower()
    if name and not name.endswith("."):
        name += "."
    return name


def _labels(name):
    name = _name(name)
    if name in {"", "."}:
        return ()
    return tuple(name[:-1].split("."))


def _canonical_key(name):
    return tuple(reversed(_labels(name)))


def _is_subdomain(name, zone):
    name_labels = _labels(name)
    zone_labels = _labels(zone)
    if not zone_labels:
        return True
    return (
        len(name_labels) >= len(zone_labels)
        and name_labels[-len(zone_labels):] == zone_labels
    )


def _key_tag(value):
    if isinstance(value, Mapping):
        for key in ("key_tag", "keyTag", "tag", "id", "key"):
            if key in value:
                return _as_int(value[key])
        if len(value) == 1:
            return _as_int(next(iter(value.values())))
        return None
    return _as_int(value)


def _coerce_tags(tags):
    if tags is None:
        return set()
    if isinstance(tags, Mapping):
        values = [tags]
    elif isinstance(tags, (str, bytes, int)):
        values = [tags]
    else:
        values = list(tags)
    result = set()
    for value in values:
        tag = _key_tag(value)
        if tag is not None:
            result.add(tag)
    return result


def _result(status, reason=None, ad=False, cd=False, ttl=None, **extra):
    result = {"status": status, "reason": reason, "ad": bool(ad), "cd": bool(cd)}
    if ttl is not None:
        result["ttl"] = ttl
    result.update(extra)
    return result


def _nsec_covers(owner, next_name, name):
    if not _name(owner) or not _name(next_name):
        return False
    owner_key = _canonical_key(owner)
    next_key = _canonical_key(next_name)
    name_key = _canonical_key(name)
    if owner_key == next_key:
        return name_key != owner_key
    if owner_key < next_key:
        return owner_key < name_key < next_key
    return name_key > owner_key or name_key < next_key


def _nsec3_hash(name, salt, iterations):
    wire = b""
    for label in _labels(name):
        encoded = label.encode("ascii")
        if len(encoded) > 255:
            return None
        wire += bytes([len(encoded)]) + encoded
    wire += b"\x00"
    if isinstance(salt, str):
        salt = salt.strip()
        if salt in {"", "-"}:
            salt_bytes = b""
        else:
            try:
                salt_bytes = bytes.fromhex(salt)
            except ValueError:
                salt_bytes = salt.encode("ascii")
    elif isinstance(salt, (bytes, bytearray)):
        salt_bytes = bytes(salt)
    else:
        salt_bytes = b""
    digest = hashlib.sha1(wire + salt_bytes).digest()
    for _ in range(max(0, iterations)):
        digest = hashlib.sha1(digest + salt_bytes).digest()
    return base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()


def _cover_names(record):
    raw = record.get("covers")
    if raw is None:
        raw = record.get("cover")
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        raw = [raw]
    elif isinstance(raw, str):
        raw = [raw]
    else:
        try:
            raw = list(raw)
        except TypeError:
            raw = [raw]
    names = []
    for value in raw:
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("owner")
        if value is not None:
            names.append(str(value).strip().lower().rstrip("."))
    return names


def _pick_failure(current, candidate):
    order = {"missing_dnskey": 0, "untrusted_key": 1, "bad_signature": 2}
    if current is None:
        return candidate
    return current if order.get(current, 9) <= order.get(candidate, 9) else candidate


def _wildcard_names_valid(qname, closest_encloser, next_closer):
    qname_labels = _labels(qname)
    closest_labels = _labels(closest_encloser)
    next_labels = _labels(next_closer)
    if len(qname_labels) <= len(closest_labels):
        return False
    if closest_labels:
        if len(next_labels) != len(closest_labels) + 1:
            return False
        if next_labels[-len(closest_labels):] != closest_labels:
            return False
    elif len(next_labels) != 1:
        return False
    if len(qname_labels) < len(next_labels):
        return False
    return qname_labels[-len(next_labels):] == next_labels


class NegativeTrustAnchor:
    def __init__(self, anchors=None, nsec3_iterations_limit=100):
        self.anchors = {}
        self.nsec3_iterations_limit = nsec3_iterations_limit
        self._rotations = {}
        self._default_anchors = set()
        self._cache = {}
        if anchors is not None:
            if isinstance(anchors, Mapping):
                for zone, tags in anchors.items():
                    self.add_anchor(zone, tags)
            else:
                self._default_anchors = _coerce_tags(anchors)

    def add_anchor(self, zone, key_tags):
        zone = _name(zone)
        tags = _coerce_tags(key_tags)
        self.anchors.setdefault(zone, set()).update(tags)
        return sorted(self.anchors[zone])

    def rotate_anchor(self, zone, new_tags):
        zone = _name(zone)
        new_tags = _coerce_tags(new_tags)
        old_tags = (
            set(self.anchors[zone])
            if zone in self.anchors
            else set(self.trust_anchor(zone))
        )
        self.anchors[zone] = old_tags | new_tags
        self._rotations[zone] = set(self._rotations.get(zone, set())) | new_tags
        return sorted(self.anchors[zone])

    def finalize_rotation(self, zone):
        zone = _name(zone)
        new_tags = self._rotations.pop(zone, None)
        if new_tags is not None:
            self.anchors[zone] = set(new_tags)
        return sorted(self.anchors.get(zone, set()))

    def _anchor_zone(self, zone):
        zone = _name(zone)
        candidates = [
            anchored
            for anchored in self.anchors
            if anchored and _is_subdomain(zone, anchored)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: len(_labels(item)))

    def trust_anchor(self, zone):
        anchored = self._anchor_zone(zone)
        if anchored is None:
            return sorted(self._default_anchors)
        return sorted(self.anchors.get(anchored, set()))

    def validate(self, zone, packet, now=0.0, cd=False):
        packet = packet or {}
        cd = _as_bool(cd) or _as_bool(packet.get("cd", False))
        if cd:
            return _result(INSECURE, "cd", ad=False, cd=True)

        signed = _as_bool(packet.get("signed", False))
        if not signed:
            return _result(INSECURE, "unsigned", ad=False, cd=cd)

        trusted = set(self.trust_anchor(zone))
        if not trusted:
            return _result(INSECURE, "no_trust_anchor", ad=False, cd=cd)

        dnskeys = _coerce_tags(
            packet.get("dnskey", packet.get("dnskeys", packet.get("keys", ())))
        )
        if not dnskeys:
            return _result(BOGUS, "missing_dnskey", ad=False, cd=cd)

        rrsets = packet.get("rrsets") or ()
        chain = packet.get("cname_chain") or packet.get("chain") or ()
        if not rrsets and not chain:
            return _result(BOGUS, "missing_rrset", ad=False, cd=cd)

        for rrset in list(rrsets) + list(chain):
            if not isinstance(rrset, Mapping):
                return _result(BOGUS, "malformed_rrset", ad=False, cd=cd)
            signatures = rrset.get("signatures")
            if signatures is None:
                signature = rrset.get("signature")
                signatures = [signature] if signature is not None else []
            elif isinstance(signatures, Mapping):
                signatures = [signatures]
            if not signatures:
                return _result(BOGUS, "missing_signature", ad=False, cd=cd)

            failure = None
            for signature in signatures:
                if not isinstance(signature, Mapping):
                    failure = failure or "bad_signature"
                    continue
                tag = _key_tag(signature)
                if tag is None or tag not in dnskeys:
                    failure = _pick_failure(failure, "missing_dnskey")
                    continue
                if tag not in trusted:
                    failure = _pick_failure(failure, "untrusted_key")
                    continue
                if not _as_bool(signature.get("valid", False)):
                    failure = _pick_failure(failure, "bad_signature")
                    continue
                failure = None
                break
            if failure is not None:
                return _result(BOGUS, failure, ad=False, cd=cd)

        return _result(SECURE, None, ad=True, cd=cd)

    def negative_proof(self, response, now=0.0):
        response = response or {}
        qname = _name(response.get("name"))
        qtype = str(response.get("type") or "NXDOMAIN").strip().upper()
        query_type = str(response.get("qtype") or response.get("rrtype") or "").strip().upper()
        nsec_records = [
            record
            for record in (response.get("nsec") or ())
            if isinstance(record, Mapping)
        ]
        nsec3_records = [
            record
            for record in (response.get("nsec3") or ())
            if isinstance(record, Mapping)
        ]

        for record in nsec3_records:
            iterations = _as_int(record.get("iterations", 0), 0)
            if iterations < 0 or (
                self.nsec3_iterations_limit is not None
                and iterations > self.nsec3_iterations_limit
            ):
                return _result(BOGUS, "nsec3_iterations", ad=False)

        wildcard = _as_bool(response.get("wildcard", False))
        if wildcard:
            closest = response.get("closest_encloser")
            next_closer = response.get("next_closer")
            if not closest or not next_closer or not _wildcard_names_valid(
                qname, closest, next_closer
            ):
                return _result(BOGUS, "wildcard_proof_incomplete", ad=False)
            targets = [_name(next_closer)]
        else:
            targets = [qname]

        if not nsec_records and not nsec3_records:
            return _result(BOGUS, "missing_negative_proof", ad=False)

        nsec_used = self._nsec_proof_records(
            nsec_records, targets, qtype, query_type
        )
        nsec3_used = self._nsec3_proof_records(
            nsec3_records, targets, qtype, query_type
        )
        if nsec_used is None and nsec3_used is None:
            return _result(BOGUS, "negative_proof_incomplete", ad=False)

        nsec3_full = (
            None
            if nsec3_used is None
            else [record for record in nsec3_used if not _as_bool(record.get("opt_out"))]
        )
        if nsec_used is not None or (nsec3_full is not None and nsec3_full):
            proof_records = []
            if nsec_used is not None:
                proof_records.extend(nsec_used)
            if nsec3_used is not None:
                proof_records.extend(nsec3_used)
            return _result(
                SECURE,
                None,
                ad=True,
                ttl=self._proof_ttl(proof_records),
            )

        return _result(
            INSECURE,
            "opt_out",
            ad=False,
            ttl=self._proof_ttl(nsec3_used or ()),
        )

    def _nsec_proof_records(self, records, targets, qtype, query_type):
        used = []
        for target in targets:
            matches = [
                record
                for record in records
                if self._nsec_matches(record, target, qtype, query_type)
            ]
            if not matches:
                return None
            used.extend(matches)
        return used

    def _nsec_matches(self, record, target, qtype, query_type):
        owner = _name(record.get("owner") or record.get("name"))
        next_name = _name(record.get("next") or record.get("next_name"))
        if not owner or not next_name:
            return False
        if qtype == "NODATA" and owner == target:
            types = {
                str(value).strip().upper()
                for value in (record.get("types") or record.get("type_bitmap") or ())
            }
            return not types or not query_type or query_type not in types
        return _nsec_covers(owner, next_name, target)

    def _nsec3_proof_records(self, records, targets, qtype, query_type):
        used = []
        for target in targets:
            matches = [
                record
                for record in records
                if self._nsec3_matches(record, target, qtype, query_type)
            ]
            if not matches:
                return None
            used.extend(matches)
        return used

    def _nsec3_matches(self, record, target, qtype, query_type):
        target_name = _name(target).rstrip(".")
        cover_names = _cover_names(record)
        if target_name in cover_names:
            return True

        owner = record.get("owner")
        next_hash = record.get("next")
        salt = record.get("salt")
        iterations = _as_int(record.get("iterations", 0), 0)
        if salt is not None and (owner is not None or next_hash is not None):
            target_hash = _nsec3_hash(target, salt, iterations)
            if target_hash:
                owner_hash = str(owner or "").strip().lower().rstrip(".")
                next_hash = str(next_hash or "").strip().lower().rstrip(".")
                if target_hash == owner_hash:
                    if qtype != "NODATA":
                        return False
                    types = {
                        str(value).strip().upper()
                        for value in (
                            record.get("types") or record.get("type_bitmap") or ()
                        )
                    }
                    return not types or not query_type or query_type not in types
                if target_hash == next_hash:
                    return False
                if owner_hash and next_hash:
                    if owner_hash < next_hash:
                        return owner_hash < target_hash < next_hash
                    return target_hash > owner_hash or target_hash < next_hash
        return False

    def _proof_ttl(self, records):
        ttls = [
            ttl
            for record in records
            for ttl in [_as_int(record.get("ttl"))]
            if ttl is not None
        ]
        return min(ttls) if ttls else None

    def store(self, name, status, ttl, now):
        status = str(status).strip().lower()
        if status not in {SECURE, INSECURE}:
            return False
        ttl = _as_int(ttl)
        now = float(now)
        if ttl is None or ttl <= 0:
            return False
        name = _name(name)
        self._cache[name] = {
            "name": name,
            "status": status,
            "ttl": ttl,
            "stored_at": now,
            "expires_at": now + ttl,
        }
        return True

    def cache(self, name, status, ttl, now):
        return self.store(name, status, ttl, now)

    def lookup(self, name, now):
        name = _name(name)
        entry = self._cache.get(name)
        if entry is None:
            return None
        if float(now) >= entry["expires_at"]:
            self._cache.pop(name, None)
            return None
        return dict(entry)

    def resolve(self, name, packet, now=0.0):
        packet = packet if isinstance(packet, Mapping) else {}
        name = _name(name)
        transport = str(packet.get("transport") or "udp").strip().lower()

        cached = self.lookup(name, now)
        if cached is not None:
            cached["source"] = "cache"
            cached["transport"] = transport
            return cached

        if _as_bool(packet.get("truncated", False)) and transport == "udp":
            return self._resolve_truncated(name, packet, now)

        return self._resolve_packet(name, packet, now)

    def _resolve_truncated(self, name, packet, now):
        tcp_packet = None
        for key in ("tcp_response", "tcp_packet", "tcp"):
            candidate = packet.get(key)
            if isinstance(candidate, Mapping):
                tcp_packet = dict(candidate)
                break
        if tcp_packet is None:
            tcp_packet = dict(packet)
            tcp_packet.pop("truncated", None)

        tcp_packet["transport"] = "tcp"
        tcp_packet["truncated"] = False
        if "zone" not in tcp_packet and packet.get("zone"):
            tcp_packet["zone"] = packet.get("zone")

        result = self._resolve_packet(name, tcp_packet, now)
        result["transport"] = "tcp"
        result["retried_over_tcp"] = True
        return result

    def _resolve_packet(self, name, packet, now):
        transport = str(packet.get("transport") or "udp").strip().lower()
        zone = packet.get("zone") or self._anchor_zone(name) or name
        response_type = str(packet.get("type") or "").strip().upper()
        negative = (
            _as_bool(packet.get("negative", False))
            or response_type in {"NXDOMAIN", "NODATA"}
            or bool(packet.get("nsec"))
            or bool(packet.get("nsec3"))
            or (
                response_type == "NOERROR"
                and not (
                    packet.get("rrsets")
                    or packet.get("cname_chain")
                    or packet.get("chain")
                )
            )
        )

        if negative:
            proof_packet = dict(packet)
            proof_packet.setdefault("name", name)
            result = self.negative_proof(proof_packet, now)
        else:
            result = self.validate(
                zone,
                packet,
                now,
                cd=_as_bool(packet.get("cd", False)),
            )

        result["transport"] = transport
        ttl = result.get("ttl")
        if ttl is None and result["status"] == SECURE:
            ttl = self._packet_ttl(packet)
        if ttl is not None and result["status"] in {SECURE, INSECURE}:
            self.store(name, result["status"], ttl, now)
        return result

    def _packet_ttl(self, packet):
        ttls = []
        rrsets = list(packet.get("rrsets") or ()) + list(
            packet.get("cname_chain") or ()
        )
        rrsets.extend(packet.get("chain") or ())
        for rrset in rrsets:
            if isinstance(rrset, Mapping):
                ttl = _as_int(rrset.get("ttl"))
                if ttl is not None:
                    ttls.append(ttl)
        direct_ttl = _as_int(packet.get("ttl"))
        if direct_ttl is not None:
            ttls.append(direct_ttl)
        return min(ttls) if ttls else None
