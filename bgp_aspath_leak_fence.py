import ipaddress


class AsPathLeakFence:
    """AS path leak fence with valley-free policy and RPKI/ASPA checks."""

    def __init__(self, local_as, relationships=None, allowed_prefixes=(),
                 max_prefix_v4=24, max_prefix_v6=48, require_aspa=True,
                 flap_half_life=300, flap_suppress=2000, flap_reuse=500):
        self.local_as = local_as
        self.relationships = dict(relationships or {})
        self.allowed_prefixes = list(allowed_prefixes)
        self.max_prefix_v4 = max_prefix_v4
        self.max_prefix_v6 = max_prefix_v6
        self.require_aspa = require_aspa
        self.flap_half_life = flap_half_life
        self.flap_suppress = flap_suppress
        self.flap_reuse = flap_reuse
        self.rib = {}
        self.flap_state = {}

    def _network(self, prefix):
        return ipaddress.ip_network(prefix, strict=False)

    def _family_of(self, route):
        family = route.get("family")
        if family:
            return family
        return "ipv6" if ":" in route.get("prefix", "") else "ipv4"

    def _expand_path(self, as_path):
        expanded = []
        for segment in as_path:
            if isinstance(segment, tuple):
                tag, members = segment
                if tag == "confed":
                    expanded.extend(members)
                else:
                    return None
            else:
                expanded.append(segment)
        return expanded

    def _has_as_set(self, as_path):
        for segment in as_path:
            if isinstance(segment, tuple) and segment[0] != "confed":
                return True
        return False

    def _prefix_allowed(self, prefix):
        net = self._network(prefix)
        for spec_text in self.allowed_prefixes:
            spec = self._network(spec_text)
            if spec.version == net.version and net.subnet_of(spec):
                return True
        return False

    def _decay(self, state, now):
        delta = now - state["at"]
        if delta > 0:
            state["penalty"] *= 0.5 ** (delta / self.flap_half_life)
            state["at"] = now
        if state["penalty"] <= self.flap_reuse:
            state["suppressed"] = False

    def _flap_state_for(self, prefix, now):
        state = self.flap_state.get(prefix)
        if state is None:
            state = {"penalty": 0.0, "at": now, "suppressed": False}
            self.flap_state[prefix] = state
        else:
            self._decay(state, now)
        return state

    def validate(self, route):
        as_path = route.get("as_path", [])
        if self._has_as_set(as_path):
            return {"ok": False, "reason": "as_set"}

        expanded_path = self._expand_path(as_path)
        if expanded_path is None:
            return {"ok": False, "reason": "as_set"}

        if route.get("rpki") == "invalid":
            return {"ok": False, "reason": "rpki_invalid"}

        if self.require_aspa and route.get("aspa") is False:
            return {"ok": False, "reason": "aspa_missing"}

        net = self._network(route["prefix"])
        limit = (
            self.max_prefix_v6
            if self._family_of(route) == "ipv6"
            else self.max_prefix_v4
        )
        if net.prefixlen > limit:
            return {"ok": False, "reason": "prefix_too_long"}

        return {
            "ok": True,
            "reason": "accepted",
            "expanded_path": expanded_path,
        }

    def import_policy(self, route):
        decision = self.validate(route)
        if not decision["ok"]:
            return decision
        if not self._prefix_allowed(route["prefix"]):
            return {"ok": False, "reason": "prefix_not_allowed"}
        return decision

    def export_policy(self, route, target_type):
        learned_from = route.get("learned_from")
        if learned_from in ("peer", "upstream") and target_type != "customer":
            return {"ok": False, "reason": "valley_free"}

        decision = self.validate(route)
        if not decision["ok"]:
            return decision
        if not self._prefix_allowed(route["prefix"]):
            return {"ok": False, "reason": "prefix_not_allowed"}
        return decision

    def update(self, route, now=0):
        decision = self.validate(route)
        if not decision["ok"]:
            return decision

        prefix = route["prefix"]
        state = self._flap_state_for(prefix, now)
        if state["suppressed"] or state["penalty"] >= self.flap_suppress:
            state["suppressed"] = True
            return {"ok": False, "reason": "flap_suppressed"}

        expanded_path = decision["expanded_path"]
        incoming = dict(route)
        incoming["as_path"] = expanded_path

        current = self.rib.get(prefix)
        if current is None:
            self.rib[prefix] = incoming
        else:
            new_pref = route.get("local_pref", 100)
            cur_pref = current.get("local_pref", 100)
            if new_pref > cur_pref or (
                new_pref == cur_pref
                and len(expanded_path) < len(current.get("as_path", []))
            ):
                self.rib[prefix] = incoming

        return {"ok": True, "reason": "accepted"}

    def withdraw(self, prefix, now=0):
        state = self._flap_state_for(prefix, now)
        state["penalty"] += 1000.0
        if state["penalty"] >= self.flap_suppress:
            state["suppressed"] = True
        return self.rib.pop(prefix, None) is not None

    def session_reset(self, neighbor_as):
        withdrawn = [
            prefix
            for prefix, route in self.rib.items()
            if route.get("neighbor_as") == neighbor_as
        ]
        for prefix in withdrawn:
            del self.rib[prefix]
        return withdrawn

    def snapshot(self):
        return {
            "rib": {prefix: dict(route) for prefix, route in self.rib.items()},
            "stats": {"installed": len(self.rib)},
        }
