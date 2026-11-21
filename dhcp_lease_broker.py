import copy
import ipaddress
import re
import threading


OPTION_NAMES = {
    12: "hostname",
    15: "domain",
    42: "ntp_servers",
    44: "netbios_name_servers",
    66: "tftp_server",
    67: "bootfile",
    81: "client_fqdn",
    82: "relay_option82",
}


class DhcpLeaseBroker:
    """DHCP lease broker with reservations, option 82 and persistence."""

    def __init__(self, pools=None, reservations=None, min_lease=60,
                 max_lease=86400, quarantine=300, offer_timeout=None):
        self.pools = list(pools or [])
        self.min_lease = int(min_lease)
        self.max_lease = int(max_lease)
        self.quarantine = int(quarantine)
        self.offer_timeout = max(
            1,
            int(offer_timeout if offer_timeout is not None else self.min_lease),
        )
        self._lock = threading.RLock()
        self._pools = [self._build_pool(pool) for pool in self.pools]
        self.reservations = {}
        for mac, value in dict(reservations or {}).items():
            if isinstance(value, dict):
                value = value.get("ip")
            if value is None:
                continue
            self.reservations[self.normalize_mac(mac)] = self._normalize_ip(value)
        self._reserved_by_ip = {
            ip: mac for mac, ip in self.reservations.items()
        }
        self._reserved_ips = set(self._reserved_by_ip)
        self._leases = {}
        self._offers = {}
        self._quarantines = {}

    def normalize_mac(self, mac):
        """Return a lower-case, colon-separated MAC address."""
        if isinstance(mac, (bytes, bytearray, memoryview)):
            raw = bytes(mac)
            if len(raw) == 6:
                return ":".join("{:02x}".format(byte) for byte in raw)
            mac = raw.decode("ascii", "ignore")
        text = str(mac).strip().lower()
        if text.startswith("0x"):
            text = text[2:]
        digits = "".join(re.findall(r"[0-9a-f]", text))
        if len(digits) != 12:
            raise ValueError("invalid MAC address: {!r}".format(mac))
        return ":".join(digits[index:index + 2]
                        for index in range(0, 12, 2))

    def offer(self, request, now=0):
        with self._lock:
            now = self._time(now)
            self._expire_state(now)
            mac, client_id = self._identity(request)
            lease = self._find_lease(mac, client_id)
            if lease is not None:
                return self._lease_reply("offer", lease, request, now)

            pending = self._find_offer(mac, client_id)
            if pending is not None:
                return self._offer_reply(pending, request, now)

            duration = self._lease_duration(request)
            pools = self._candidate_pools(request)
            if not pools:
                return self._nak("no_pool", request)

            reserved_ip = self._reservation_for(mac, lease)
            if reserved_ip is not None:
                if not self._ip_available(reserved_ip, mac, client_id,
                                          now, pools):
                    return self._nak("reservation_conflict", request)
                ip = reserved_ip
                pool = self._pool_for_ip(ip, pools) or self._pool_for_ip(
                    ip, self._pools)
            else:
                requested_ip = self._requested_ip(request)
                ip = None
                pool = None
                if requested_ip is not None:
                    pool = self._pool_for_ip(requested_ip, pools)
                    if (pool is not None and
                            self._ip_available(requested_ip, mac, client_id,
                                               now, pools)):
                        ip = requested_ip
                if ip is None:
                    ip, pool = self._first_available(pools, mac, client_id, now)
            if ip is None or pool is None:
                return self._nak("pool_exhausted", request)

            offer = {
                "ip": ip,
                "mac": mac,
                "macs": [mac] if mac else [],
                "client_id": client_id,
                "client_ids": [client_id] if client_id else [],
                "starts_at": now,
                "expires_at": now + self.offer_timeout,
                "lease_time": duration,
                "subnet": pool["subnet"],
                "reserved": reserved_ip is not None,
            }
            self._offers[ip] = offer
            return self._offer_reply(offer, request, now)

    def request(self, request, now=0):
        with self._lock:
            now = self._time(now)
            self._expire_state(now)
            mac, client_id = self._identity(request)
            duration = self._lease_duration(request)
            pools = self._candidate_pools(request)
            requested_ip = self._requested_ip(request)
            lease = self._find_lease(mac, client_id)
            pending = self._find_offer(mac, client_id)
            reserved_ip = self._reservation_for(
                mac, lease or pending
            )

            if reserved_ip is not None:
                if requested_ip is not None and requested_ip != reserved_ip:
                    return self._nak("reservation_conflict", request)
                if lease is not None:
                    if lease["ip"] != reserved_ip:
                        self._leases.pop(lease["ip"], None)
                        lease = None
                    else:
                        return self._renew(lease, request, now, duration)
                if not self._ip_available(reserved_ip, mac, client_id,
                                          now, pools or self._pools):
                    return self._nak("reservation_conflict", request)
                pool = (self._pool_for_ip(reserved_ip, pools or self._pools)
                        or self._pool_for_ip(reserved_ip, self._pools))
                lease = self._new_lease(
                    reserved_ip, mac, client_id, now, duration, pool, True
                )
                return self._finish_ack(lease, request, now)

            if lease is not None:
                if requested_ip is not None and requested_ip != lease["ip"]:
                    if not self._requested_ip_is_usable(
                            requested_ip, mac, client_id, now, pools):
                        return self._nak("reservation_conflict", request)
                    self._leases.pop(lease["ip"], None)
                    pool = self._pool_for_ip(requested_ip, pools or self._pools)
                    lease = self._new_lease(
                        requested_ip, mac, client_id, now, duration, pool, False
                    )
                    return self._finish_ack(lease, request, now)
                return self._renew(lease, request, now, duration)

            ip = None
            pool = None
            reserved = False

            if requested_ip is not None:
                if not self._requested_ip_is_usable(
                        requested_ip, mac, client_id, now, pools):
                    reason = ("invalid_address"
                              if self._pool_for_ip(
                                  requested_ip, pools or self._pools) is None
                              else "reservation_conflict")
                    return self._nak(reason, request)
                ip = requested_ip
                pool = self._pool_for_ip(ip, pools or self._pools)
            elif pending is not None:
                pending_ip = pending["ip"]
                if self._ip_available(pending_ip, mac, client_id, now,
                                      pools or self._pools):
                    ip = pending_ip
                    pool = (self._pool_for_ip(ip, pools or self._pools)
                            or self._pool_for_ip(ip, self._pools))
            if ip is None:
                if not pools:
                    return self._nak("no_pool", request)
                ip, pool = self._first_available(
                    pools, mac, client_id, now
                )
            if ip is None or pool is None:
                return self._nak("pool_exhausted", request)

            lease = self._new_lease(
                ip, mac, client_id, now, duration, pool, reserved
            )
            return self._finish_ack(lease, request, now)

    def decline(self, request, now=0):
        with self._lock:
            now = self._time(now)
            self._expire_state(now)
            mac, client_id = self._identity(request)
            ip = self._requested_ip(request)
            lease = self._find_lease(mac, client_id)
            if ip is None and lease is not None:
                ip = lease["ip"]

            if ip is not None:
                held = self._leases.get(ip)
                if held is not None and self._entry_matches(
                        held, mac, client_id):
                    self._leases.pop(ip, None)
                offered = self._offers.get(ip)
                if offered is not None and self._entry_matches(
                        offered, mac, client_id):
                    self._offers.pop(ip, None)
                self._remove_offers(mac, client_id)
                self._quarantines[ip] = now + self.quarantine

            return {
                "type": "decline",
                "ip": ip,
                "quarantined_until": (
                    self._quarantines.get(ip) if ip is not None else None
                ),
                "reply": self._reply_mode(request),
            }

    def release(self, request, now=0):
        with self._lock:
            now = self._time(now)
            self._expire_state(now)
            mac, client_id = self._identity(request)
            ip = self._requested_ip(request)
            lease = self._find_lease(mac, client_id)
            if ip is None and lease is not None:
                ip = lease["ip"]

            if ip is not None:
                held = self._leases.get(ip)
                if held is not None and self._entry_matches(
                        held, mac, client_id):
                    self._leases.pop(ip, None)
                offered = self._offers.get(ip)
                if offered is not None and self._entry_matches(
                        offered, mac, client_id):
                    self._offers.pop(ip, None)
            self._remove_offers(mac, client_id, ip)

            return {
                "type": "release",
                "ip": ip,
                "reply": self._reply_mode(request),
            }

    def expire(self, now=0):
        with self._lock:
            return self._expire_state(self._time(now))

    def leases(self):
        with self._lock:
            return {
                ip: copy.deepcopy(self._leases[ip])
                for ip in sorted(self._leases, key=self._ip_sort_key)
            }

    def dump_state(self):
        with self._lock:
            return {
                "version": 1,
                "leases": self._json_safe(self._leases),
                "offers": self._json_safe(self._offers),
                "quarantines": self._json_safe(self._quarantines),
                "reservations": dict(self.reservations),
            }

    def load_state(self, state):
        with self._lock:
            if not isinstance(state, dict):
                raise ValueError("state must be a mapping")
            state = self._json_restore(state)
            leases = state.get("leases", {})
            if not isinstance(leases, dict):
                leases = {}
            offers = state.get("offers", {})
            if not isinstance(offers, dict):
                offers = {}
            quarantines = state.get("quarantines", {})
            if not isinstance(quarantines, dict):
                quarantines = {}

            self._leases = {}
            for ip, lease in leases.items():
                if not isinstance(lease, dict):
                    continue
                entry = copy.deepcopy(lease)
                ip = self._normalize_ip(entry.get("ip", ip))
                entry["ip"] = ip
                self._normalize_entry_identity(entry)
                self._leases[ip] = entry

            self._offers = {}
            for ip, offer in offers.items():
                if not isinstance(offer, dict):
                    continue
                entry = copy.deepcopy(offer)
                ip = self._normalize_ip(entry.get("ip", ip))
                entry["ip"] = ip
                self._normalize_entry_identity(entry)
                self._offers[ip] = entry

            self._quarantines = {}
            for ip, expires_at in quarantines.items():
                self._quarantines[self._normalize_ip(ip)] = expires_at

            stored_reservations = state.get("reservations") or {}
            merged = {}
            for mac, ip in stored_reservations.items():
                merged[self.normalize_mac(mac)] = self._normalize_ip(ip)
            merged.update(self.reservations)
            self.reservations = merged
            self._reserved_by_ip = {
                ip: mac for mac, ip in self.reservations.items()
            }
            self._reserved_ips = set(self._reserved_by_ip)
            return self

    def stats(self):
        with self._lock:
            pool_size = sum(
                pool["_end"] - pool["_start"] + 1
                for pool in self._pools
                if pool["_end"] >= pool["_start"]
            )
            unavailable = set(self._leases)
            unavailable.update(self._offers)
            unavailable.update(self._quarantines)
            unavailable.update(self._reserved_ips)
            available = 0
            for pool in self._pools:
                for value in range(pool["_start"], pool["_end"] + 1):
                    if str(ipaddress.ip_address(value)) not in unavailable:
                        available += 1
            return {
                "active_leases": len(self._leases),
                "pending_offers": len(self._offers),
                "quarantined": len(self._quarantines),
                "reserved": len(self.reservations),
                "pool_size": pool_size,
                "available": available,
            }

    def _build_pool(self, raw):
        if not isinstance(raw, dict):
            raw = {"subnet": str(raw)}
        pool = dict(raw)
        subnet = pool.get("subnet")
        start = pool.get("start")
        end = pool.get("end")
        if subnet is None:
            if start is None:
                raise ValueError("pool needs a subnet or start address")
            subnet = "{}/24".format(self._normalize_ip(start))
        network = ipaddress.ip_network(str(subnet), strict=False)
        start_ip = (ipaddress.ip_address(start) if start is not None
                    else network.network_address + 1)
        end_ip = (ipaddress.ip_address(end) if end is not None
                  else network.broadcast_address - 1)
        pool["subnet"] = str(network)
        pool["_network"] = network
        pool["_start"] = int(start_ip)
        pool["_end"] = int(end_ip)
        return pool

    def _time(self, now):
        return 0 if now is None else now

    def _normalize_ip(self, value):
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray, memoryview)):
            value = bytes(value).decode("ascii")
        text = str(value).strip()
        if text in ("", "0.0.0.0"):
            return None
        return str(ipaddress.ip_address(text))

    def _requested_ip(self, request):
        return self._normalize_ip(
            request.get("requested_ip", request.get("ip"))
        )

    def _identity(self, request):
        mac_value = request.get("mac")
        mac = self.normalize_mac(mac_value) if mac_value else None
        return mac, self._normalize_client_id(request.get("client_id"))

    def _normalize_client_id(self, client_id):
        if client_id is None:
            return None
        if isinstance(client_id, (bytes, bytearray, memoryview)):
            return "0x" + bytes(client_id).hex()
        text = str(client_id).strip()
        return text or None

    def _lease_duration(self, request):
        value = request.get("lease_time")
        if value is None or value == float("inf"):
            value = self.max_lease
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = self.max_lease
        if value >= 0xFFFFFFFF:
            value = self.max_lease
        return max(self.min_lease, min(self.max_lease, value))

    def _relay_info(self, request):
        relay = request.get("relay") or {}
        if not isinstance(relay, dict):
            relay = {}
        giaddr = self._normalize_ip(
            relay.get("giaddr", request.get("giaddr"))
        )
        option82 = None
        found = False
        for key in ("option82", "option_82", "relay_option82",
                    "agent_circuit_id", "circuit_id"):
            if key in relay:
                option82 = relay[key]
                found = True
                break
        if not found and "relay_option82" in request:
            option82 = request.get("relay_option82")
        return giaddr, option82

    def _candidate_pools(self, request):
        giaddr, option82 = self._relay_info(request)
        if option82 is not None:
            selected = [
                pool for pool in self._pools
                if self._pool_matches_option82(pool, option82)
            ]
            if selected:
                return selected
            hint = self._option82_subnet(option82)
            if hint is not None:
                selected = [
                    pool for pool in self._pools
                    if pool["_network"] == hint
                    or (hint.version == pool["_network"].version
                        and hint.subnet_of(pool["_network"]))
                ]
                if selected:
                    return selected
        if giaddr is not None:
            address = ipaddress.ip_address(giaddr)
            selected = [
                pool for pool in self._pools
                if address in pool["_network"]
            ]
            if not selected:
                return []
            longest = max(pool["_network"].prefixlen for pool in selected)
            return [
                pool for pool in selected
                if pool["_network"].prefixlen == longest
            ]
        return list(self._pools)

    def _pool_matches_option82(self, pool, option82):
        for key in ("option82", "option_82", "relay_option82",
                    "circuit_id", "agent_circuit_id"):
            if key not in pool:
                continue
            value = pool[key]
            if value == option82:
                return True
            if isinstance(option82, dict):
                for option_key in ("circuit_id", "circuitId",
                                   "agent_circuit_id", "subnet"):
                    if option82.get(option_key) == value:
                        return True
            if isinstance(value, dict):
                if value.get("subnet") == option82:
                    return True
        return False

    def _option82_subnet(self, option82):
        if isinstance(option82, dict):
            for key in ("subnet", "link_selection", "link_selection_subnet",
                        "giaddr"):
                value = option82.get(key)
                if value is not None:
                    try:
                        return ipaddress.ip_network(
                            str(value), strict=False
                        )
                    except ValueError:
                        continue
        return None

    def _pool_for_ip(self, ip, pools):
        if ip is None:
            return None
        address = ipaddress.ip_address(ip)
        value = int(address)
        for pool in pools:
            if (address in pool["_network"]
                    and pool["_start"] <= value <= pool["_end"]):
                return pool
        return None

    def _first_available(self, pools, mac, client_id, now):
        for pool in pools:
            for value in range(pool["_start"], pool["_end"] + 1):
                ip = str(ipaddress.ip_address(value))
                if ip in self._reserved_ips:
                    continue
                if self._ip_available(ip, mac, client_id, now, pools):
                    return ip, pool
        return None, None

    def _ip_available(self, ip, mac, client_id, now, pools):
        if self._is_quarantined(ip, now):
            return False
        owner = self._reserved_by_ip.get(ip)
        if owner is not None and owner != mac:
            return False
        lease = self._leases.get(ip)
        if lease is not None and not self._entry_matches(
                lease, mac, client_id):
            return False
        offer = self._offers.get(ip)
        if offer is not None and not self._entry_matches(
                offer, mac, client_id):
            return False
        return True

    def _requested_ip_is_usable(self, ip, mac, client_id, now, pools):
        if self._is_quarantined(ip, now):
            return False
        if self._pool_for_ip(ip, pools or self._pools) is None:
            return False
        return self._ip_available(ip, mac, client_id, now, pools)

    def _is_quarantined(self, ip, now):
        expires_at = self._quarantines.get(ip)
        return expires_at is not None and expires_at > now

    def _reservation_for(self, mac, entry):
        if mac is not None and mac in self.reservations:
            return self.reservations[mac]
        if entry is not None:
            for candidate in entry.get("macs", []):
                if candidate in self.reservations:
                    return self.reservations[candidate]
            entry_mac = entry.get("mac")
            if entry_mac in self.reservations:
                return self.reservations[entry_mac]
        return None

    def _new_lease(self, ip, mac, client_id, now, duration, pool, reserved):
        lease = {
            "ip": ip,
            "mac": mac,
            "macs": [mac] if mac else [],
            "client_id": client_id,
            "client_ids": [client_id] if client_id else [],
            "starts_at": now,
            "updated_at": now,
            "expires_at": now + duration,
            "lease_time": duration,
            "subnet": (pool["subnet"] if pool is not None
                       else self._subnet_for_ip(ip)),
            "reserved": reserved,
        }
        self._leases[ip] = lease
        self._remove_offers(mac, client_id, ip)
        return lease

    def _renew(self, lease, request, now, duration):
        self._add_identity(lease, *self._identity(request))
        lease["updated_at"] = now
        lease["lease_time"] = duration
        lease["expires_at"] = lease["expires_at"] + duration
        return self._finish_ack(lease, request, now)

    def _finish_ack(self, lease, request, now):
        self._remove_offers(
            lease.get("mac"), lease.get("client_id"), lease["ip"]
        )
        return self._lease_reply("ack", lease, request, now)

    def _subnet_for_ip(self, ip):
        address = ipaddress.ip_address(ip)
        for pool in self._pools:
            if address in pool["_network"]:
                return pool["subnet"]
        return str(ipaddress.ip_network("{}/24".format(ip), strict=False))

    def _entry_matches(self, entry, mac, client_id):
        if mac is not None:
            if entry.get("mac") == mac or mac in entry.get("macs", []):
                return True
        if client_id is not None:
            ids = entry.get("client_ids") or []
            if client_id == entry.get("client_id") or client_id in ids:
                return True
        return False

    def _find_lease(self, mac, client_id):
        matches = [
            lease for lease in self._leases.values()
            if self._entry_matches(lease, mac, client_id)
        ]
        return self._prefer_mac(matches, mac)

    def _find_offer(self, mac, client_id):
        matches = [
            offer for offer in self._offers.values()
            if self._entry_matches(offer, mac, client_id)
        ]
        return self._prefer_mac(matches, mac)

    def _prefer_mac(self, matches, mac):
        if not matches:
            return None
        if mac is not None:
            for entry in matches:
                if entry.get("mac") == mac:
                    return entry
        return matches[0]

    def _add_identity(self, entry, mac, client_id):
        if mac is not None:
            if not entry.get("mac"):
                entry["mac"] = mac
            macs = entry.setdefault("macs", [])
            if mac not in macs:
                macs.append(mac)
        if client_id is not None:
            if not entry.get("client_id"):
                entry["client_id"] = client_id
            ids = entry.setdefault("client_ids", [])
            if client_id not in ids:
                ids.append(client_id)

    def _normalize_entry_identity(self, entry):
        mac = entry.get("mac")
        if mac:
            mac = self.normalize_mac(mac)
            entry["mac"] = mac
        macs = []
        for value in entry.get("macs", []):
            try:
                normalized = self.normalize_mac(value)
            except ValueError:
                continue
            if normalized not in macs:
                macs.append(normalized)
        if mac and mac not in macs:
            macs.insert(0, mac)
        entry["macs"] = macs

        client_id = entry.get("client_id")
        if client_id is not None:
            client_id = self._normalize_client_id(client_id)
            entry["client_id"] = client_id
        ids = []
        for value in entry.get("client_ids", []):
            normalized = self._normalize_client_id(value)
            if normalized is not None and normalized not in ids:
                ids.append(normalized)
        if client_id and client_id not in ids:
            ids.insert(0, client_id)
        entry["client_ids"] = ids

    def _remove_offers(self, mac, client_id, ip=None):
        if ip is None:
            remove = [
                offer_ip for offer_ip, offer in self._offers.items()
                if self._entry_matches(offer, mac, client_id)
            ]
        else:
            remove = []
            offer = self._offers.get(ip)
            if offer is not None and (
                    self._entry_matches(offer, mac, client_id)
                    or mac is None and client_id is None):
                remove.append(ip)
            remove.extend(
                offer_ip for offer_ip, offer in self._offers.items()
                if self._entry_matches(offer, mac, client_id)
            )
        for offer_ip in set(remove):
            self._offers.pop(offer_ip, None)

    def _expire_state(self, now):
        expired = [
            ip for ip, lease in self._leases.items()
            if lease.get("expires_at", 0) <= now
        ]
        for ip in expired:
            self._leases.pop(ip, None)

        for ip, offer in list(self._offers.items()):
            if offer.get("expires_at", 0) <= now:
                self._offers.pop(ip, None)

        for ip, expires_at in list(self._quarantines.items()):
            if expires_at <= now:
                self._quarantines.pop(ip, None)
        return len(expired)

    def _lease_reply(self, kind, lease, request, now):
        return {
            "type": kind,
            "ip": lease["ip"],
            "mac": lease.get("mac"),
            "client_id": lease.get("client_id"),
            "lease_time": lease.get("lease_time"),
            "expires_at": lease.get("expires_at"),
            "starts_at": lease.get("starts_at"),
            "subnet": lease.get("subnet"),
            "reserved": lease.get("reserved", False),
            "relay_option82": self._relay_info(request)[1],
            "parsed_options": self._parsed_options(request),
            "reply": self._reply_mode(request),
        }

    def _offer_reply(self, offer, request, now):
        return {
            "type": "offer",
            "ip": offer["ip"],
            "mac": offer.get("mac"),
            "client_id": offer.get("client_id"),
            "lease_time": offer.get("lease_time"),
            "expires_at": offer.get("expires_at"),
            "offer_expires_at": offer.get("expires_at"),
            "subnet": offer.get("subnet"),
            "reserved": offer.get("reserved", False),
            "relay_option82": self._relay_info(request)[1],
            "parsed_options": self._parsed_options(request),
            "reply": self._reply_mode(request),
        }

    def _nak(self, reason, request):
        return {
            "type": "nak",
            "reason": reason,
            "relay_option82": self._relay_info(request)[1],
            "parsed_options": self._parsed_options(request),
            "reply": self._reply_mode(request),
        }

    def _reply_mode(self, request):
        return "broadcast" if request.get("broadcast") else "unicast"

    def _parsed_options(self, request):
        parsed = {}
        options = request.get("options")
        if isinstance(options, dict):
            parsed.update(self._parse_option_field(options))
        overload = request.get("overload")
        if not overload:
            return parsed
        if isinstance(overload, dict):
            for field, value in overload.items():
                if field in ("file", "sname"):
                    parsed.update(self._parse_option_field(value))
                elif field not in ("overload",):
                    parsed.update(self._parse_option_field({field: value}))
        else:
            parsed.update(self._parse_option_field(overload))
        return parsed

    def _parse_option_field(self, field):
        parsed = {}
        if isinstance(field, (bytes, bytearray, memoryview)):
            return self._parse_option_bytes(bytes(field))
        if isinstance(field, dict):
            if "code" in field and "value" in field:
                field = [field]
            else:
                for key, value in field.items():
                    name = self._option_name(key)
                    if name is not None:
                        parsed[name] = self._option_value(value)
                return parsed
        if isinstance(field, (list, tuple)):
            for item in field:
                if isinstance(item, dict):
                    name = item.get("name")
                    if name is None:
                        name = self._option_name(item.get("code"))
                    if name is not None:
                        parsed[name] = self._option_value(item.get("value"))
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    name = self._option_name(item[0])
                    if name is not None:
                        parsed[name] = self._option_value(item[1])
        return parsed

    def _parse_option_bytes(self, data):
        parsed = {}
        index = 0
        while index < len(data):
            code = data[index]
            index += 1
            if code == 0:
                continue
            if code == 255 or index >= len(data):
                break
            length = data[index]
            index += 1
            value = data[index:index + length]
            index += length
            name = self._option_name(code)
            if name is not None:
                parsed[name] = self._option_value(value)
        return parsed

    def _option_name(self, key):
        if isinstance(key, int):
            return OPTION_NAMES.get(key)
        text = str(key).strip()
        if text.isdigit():
            return OPTION_NAMES.get(int(text))
        return text or None

    def _option_value(self, value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("latin-1")
        return value

    def _json_safe(self, value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"__bytes__": bytes(value).hex()}
        if isinstance(value, dict):
            return {
                str(key): self._json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._json_safe(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)

    def _json_restore(self, value):
        if isinstance(value, dict):
            if set(value) == {"__bytes__"}:
                return bytes.fromhex(value["__bytes__"])
            return {
                key: self._json_restore(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._json_restore(item) for item in value]
        return value

    def _ip_sort_key(self, ip):
        try:
            return (0, int(ipaddress.ip_address(ip)))
        except ValueError:
            return (1, str(ip))
