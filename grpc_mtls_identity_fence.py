import copy


class MtlsIdentityFence:
    def __init__(self, trust_domain="cluster.local"):
        self.trust_domain = trust_domain
        self.generation = 0
        self.valid_serials = set()
        self.revoked_serials = set()
        self.cache = {}
        self.connections = {}
        self.pools = {}
        self.audit = []
        self.audit_seq = 0
        self.checked = 0
        self.allowed = 0
        self.denied = 0

    def on_rotation(self, generation, valid_serials):
        self.generation = generation
        self.valid_serials = set(valid_serials)
        self.cache.clear()

    def revoke(self, serial):
        self.revoked_serials.add(serial)
        for key in [k for k, v in self.cache.items() if v.get("serial") == serial]:
            del self.cache[key]

    def _parse_spiffe(self, spiffe_id):
        if not isinstance(spiffe_id, str):
            return None, "invalid_spiffe"
        prefix = "spiffe://"
        if not spiffe_id.startswith(prefix):
            return None, "invalid_spiffe"
        rest = spiffe_id[len(prefix):]
        parts = rest.split("/")
        if len(parts) < 1 or not parts[0]:
            return None, "invalid_spiffe"
        domain = parts[0]
        if domain != self.trust_domain:
            return None, "trust_domain"
        path = parts[1:]
        if len(path) != 4 or path[0] != "ns" or path[2] != "sa" or not path[1] or not path[3]:
            return None, "invalid_spiffe"
        return {"trust_domain": domain, "namespace": path[1], "service": path[3]}, None

    def _record(self, decision, reason, identity=None):
        self.checked += 1
        if decision == "allow":
            self.allowed += 1
        else:
            self.denied += 1
        self.audit_seq += 1
        self.audit.append({
            "seq": self.audit_seq,
            "decision": decision,
            "reason": reason,
            "identity": identity,
            "generation": self.generation,
        })

    def _deny(self, reason, identity=None):
        self._record("deny", reason, identity)
        return {"ok": False, "reason": reason}

    def check_peer(self, peer, now=0.0):
        if not isinstance(peer, dict):
            return self._deny("invalid_peer")
        spiffe_id = peer.get("spiffe_id")
        if not peer.get("mtls"):
            return self._deny("downgrade", spiffe_id)
        parsed, err = self._parse_spiffe(spiffe_id)
        if err:
            return self._deny(err, spiffe_id)
        serial = peer.get("serial")
        if serial is None:
            return self._deny("missing_serial", spiffe_id)
        if serial in self.revoked_serials:
            return self._deny("revoked", spiffe_id)
        if serial not in self.valid_serials:
            return self._deny("stale_cert", spiffe_id)
        sans = peer.get("sans")
        if not isinstance(sans, list):
            return self._deny("invalid_san", spiffe_id)
        if not sans:
            return self._deny("missing_san", spiffe_id)
        for san in sans:
            _, san_err = self._parse_spiffe(san)
            if san_err == "trust_domain":
                return self._deny("trust_domain", spiffe_id)
            if san_err:
                return self._deny("invalid_san", spiffe_id)
            if san != spiffe_id:
                return self._deny("san_mismatch", spiffe_id)
        jwt_spiffe = peer.get("jwt_spiffe")
        if jwt_spiffe is not None and jwt_spiffe != spiffe_id:
            return self._deny("jwt_conflict", spiffe_id)
        chain = peer.get("chain") or []
        if not isinstance(chain, list):
            return self._deny("bad_chain", spiffe_id)
        for entry in chain:
            if not isinstance(entry, dict) or entry.get("ca") is not True:
                return self._deny("bad_chain", spiffe_id)
        expires_at = peer.get("expires_at")
        if expires_at is not None and now >= expires_at:
            return self._deny("expired", spiffe_id)
        self.cache[spiffe_id] = {"serial": serial, "generation": self.generation}
        self._record("allow", "ok", spiffe_id)
        return {"ok": True, "identity": spiffe_id,
                "namespace": parsed["namespace"], "service": parsed["service"]}

    def authorize(self, identity, policy, method, path, now=0.0):
        parsed, err = self._parse_spiffe(identity)
        if err or not policy:
            return False
        for rule in policy:
            if not isinstance(rule, dict):
                continue
            methods = rule.get("methods") or []
            paths = rule.get("paths") or []
            identities = rule.get("identities") or []
            if method not in methods:
                continue
            if identity not in identities:
                continue
            if any(self._path_matches(pattern, path) for pattern in paths):
                return True
        return False

    @staticmethod
    def _path_matches(pattern, path):
        if not isinstance(pattern, str) or not isinstance(path, str):
            return False
        if pattern.endswith("*"):
            return path.startswith(pattern[:-1])
        return pattern == path

    def bind_connection(self, connection_id, identity):
        parsed, err = self._parse_spiffe(identity)
        if err:
            raise ValueError("invalid identity: %s" % err)
        existing = self.connections.get(connection_id)
        if existing is not None and existing != identity:
            raise ValueError("connection %s already bound to %s" % (connection_id, existing))
        self.connections[connection_id] = identity
        pool_key = "%s|%s|%s" % (parsed["trust_domain"], parsed["namespace"], parsed["service"])
        self.pools.setdefault(pool_key, set()).add(connection_id)
        return {"pool_key": pool_key, "identity": identity}

    def state_dump(self):
        return {
            "generation": self.generation,
            "checked": self.checked,
            "allowed": self.allowed,
            "denied": self.denied,
            "cache_size": len(self.cache),
            "audit": copy.deepcopy(self.audit),
        }
