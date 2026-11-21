class BgpPrefixGuard:
    """BGP prefix admission guard with ROA, loop, flap and next-hop checks."""

    def __init__(self, local_as, roas=None, max_prefixes=1000,
                 allow_default=False, require_roa=True, flap_half_life=300,
                 flap_suppress=2000, flap_reuse=500):
        self.local_as = local_as
        self.roas = list(roas or [])
        self.max_prefixes = max_prefixes
        self.allow_default = allow_default
        self.require_roa = require_roa
        self.flap_half_life = flap_half_life
        self.flap_suppress = flap_suppress
        self.flap_reuse = flap_reuse

    def parse_as(self, value):
        raise NotImplementedError

    def announce(self, route, next_hop_reachable=True, now=0):
        raise NotImplementedError

    def withdraw(self, prefix, now=0):
        raise NotImplementedError

    def session_reset(self):
        raise NotImplementedError

    def best(self, prefix):
        raise NotImplementedError

    def rib(self):
        raise NotImplementedError

    def fib(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
import ipaddress


AS_TRANS = 1 << 16
MAX_ASN = (1 << 32) - 1

# RFC 7999 blackhole community, in both "high:low" and integer encodings.
BLACKHOLE_COMMUNITIES = {"65535:666", str(65535 * AS_TRANS + 666),
                         65535 * AS_TRANS + 666}

FLAP_PENALTY = 1000


def _default_decay(penalty, elapsed, half_life):
    """Exponential half-life recovery curve used by route flap damping."""
    if half_life <= 0:
        return penalty
    return penalty * (0.5 ** (elapsed / half_life))


class BgpPrefixGuard:
    """BGP prefix admission guard with ROA, loop, flap and next-hop checks."""

    def __init__(self, local_as, roas=None, max_prefixes=1000,
                 allow_default=False, require_roa=True, flap_half_life=300,
                 flap_suppress=2000, flap_reuse=500,
                 flap_decay=None, blackhole_communities=None):
        self.local_as = self.parse_as(local_as)
        self.roas = [self._normalize_roa(roa) for roa in (roas or [])]
        self.max_prefixes = max_prefixes
        self.allow_default = allow_default
        self.require_roa = require_roa
        self.flap_half_life = flap_half_life
        self.flap_suppress = flap_suppress
        self.flap_reuse = flap_reuse
        # Configurable recovery curve: callable(penalty, elapsed, half_life).
        self.flap_decay = flap_decay or _default_decay
        self.blackhole_communities = set(
            BLACKHOLE_COMMUNITIES
            if blackhole_communities is None
            else blackhole_communities
        )

        # prefix -> list of candidate route records (control plane RIB)
        self._routes = {}
        # prefix -> best forwarding record (data plane FIB)
        self._fib = {}
        # Cumulative admitted prefixes; deliberately kept across resets.
        self._seen_prefixes = set()
        # prefix -> {"penalty": float, "updated": int}
        self._flap = {}
        self._session_resets = 0
        self._accepted_count = 0
        self._rejected_count = 0
        self._suppressed_count = 0
        # Audit trail of every routing decision and its reason.
        self.decisions = []

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def parse_as(value):
        """Parse asplain (65001) and four-byte asdot (1.10) AS notation."""
        if isinstance(value, bool):
            raise ValueError("invalid AS number: %r" % (value,))
        if isinstance(value, int):
            asn = value
        elif isinstance(value, str):
            text = value.strip()
            if "." in text:
                parts = text.split(".")
                if len(parts) != 2:
                    raise ValueError("invalid asdot AS: %r" % (value,))
                high, low = parts
                if not (high.isdigit() and low.isdigit()):
                    raise ValueError("invalid asdot AS: %r" % (value,))
                high, low = int(high), int(low)
                if high > 0xFFFF or low > 0xFFFF:
                    raise ValueError("asdot component out of range: %r"
                                     % (value,))
                asn = high * AS_TRANS + low
            elif text.isdigit():
                asn = int(text)
            else:
                raise ValueError("invalid AS number: %r" % (value,))
        else:
            raise ValueError("invalid AS number: %r" % (value,))
        if not 0 <= asn <= MAX_ASN:
            raise ValueError("AS number out of range: %r" % (value,))
        return asn

    def _normalize_roa(self, roa):
        return {
            "network": ipaddress.ip_network(roa["prefix"], strict=False),
            "max_length": int(roa["max_length"]),
            "origin_as": self.parse_as(roa["origin_as"]),
        }

    @staticmethod
    def _is_as_set(element):
        # ("set", [65001, 65002]) or {"set": [...]}
        if isinstance(element, tuple) and len(element) == 2 \
                and element[0] == "set":
            return True
        if isinstance(element, dict) and "set" in element:
            return True
        return False

    def _normalize_path(self, as_path):
        """Return (path, as_set_found). AS-SETs are flagged, not expanded."""
        normalized = []
        as_set = False
        for element in as_path or []:
            if self._is_as_set(element):
                as_set = True
                continue
            normalized.append(self.parse_as(element))
        return normalized, as_set

    # ------------------------------------------------------------------
    # Flap damping
    # ------------------------------------------------------------------
    def _decay_flap(self, prefix, now):
        state = self._flap.get(prefix)
        if state is None:
            return 0.0
        elapsed = now - state["updated"]
        if elapsed < 0:
            elapsed = 0
        penalty = self.flap_decay(state["penalty"], elapsed,
                                  self.flap_half_life)
        if penalty <= self.flap_reuse:
            self._flap.pop(prefix, None)
            return 0.0
        state["penalty"] = penalty
        state["updated"] = now
        return penalty

    # ------------------------------------------------------------------
    # ROA / origin validation
    # ------------------------------------------------------------------
    def _validate_roa(self, network, origin_as):
        covering = [
            roa for roa in self.roas
            if roa["network"].version == network.version
            and network.subnet_of(roa["network"])
        ]
        if not covering:
            if self.require_roa:
                return "no_roa"
            return None
        # Max prefix length is enforced independently of the origin: a more
        # specific route is invalid even when its origin matches a ROA.
        length_ok = [roa for roa in covering
                     if network.prefixlen <= roa["max_length"]]
        if not length_ok:
            return "too_specific"
        if not any(roa["origin_as"] == origin_as for roa in length_ok):
            return "origin_mismatch"
        return None

    # ------------------------------------------------------------------
    # Route selection
    # ------------------------------------------------------------------
    def _select_best(self, prefix):
        candidates = self._routes.get(prefix, [])
        if not candidates:
            return None
        external = [route for route in candidates
                    if route["peer_type"] == "external"]
        # External peers always win over internal ones; earliest wins ties.
        return external[0] if external else candidates[0]

    def _sync_fib(self, prefix):
        best = self._select_best(prefix)
        if best is None or best["action"] == "discard":
            self._fib.pop(prefix, None)
        else:
            self._fib[prefix] = best

    def _decision(self, accepted, reason, route, normalized_path=None,
                  action=None):
        result = {
            "accepted": accepted,
            "reason": reason,
            "action": action,
            "prefix": route.get("prefix"),
            "origin_as": route.get("origin_as"),
            "as_path": (normalized_path
                        if normalized_path is not None
                        else route.get("as_path")),
            "next_hop": route.get("next_hop"),
            "peer_type": route.get("peer_type"),
            "neighbor_as": route.get("neighbor_as"),
            "communities": list(route.get("communities") or []),
        }
        self.decisions.append(dict(result))
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def announce(self, route, next_hop_reachable=True, now=0):
        prefix = route["prefix"]
        normalized_path, as_set = self._normalize_path(route.get("as_path"))
        origin_as = self.parse_as(route["origin_as"])

        # 1. AS-SET paths have an explicit rule: reject, never skip checks.
        if as_set:
            self._rejected_count += 1
            return self._decision(False, "as_set", route, normalized_path)

        # 2. Loop detection scans the whole AS path, not just the neighbor.
        if self.local_as in normalized_path:
            self._rejected_count += 1
            return self._decision(False, "as_loop", route, normalized_path)

        # 3. Default route is denied unless explicitly allowed.
        network = ipaddress.ip_network(prefix, strict=False)
        if network.prefixlen == 0 and not self.allow_default:
            self._rejected_count += 1
            return self._decision(False, "default_route", route,
                                  normalized_path)

        # 4. ROA: covering prefix, max length and origin must all match.
        roa_reason = self._validate_roa(network, origin_as)
        if roa_reason is not None:
            # Invalid origins are rejected per policy; logging alone is not
            # an acceptable outcome.
            self._rejected_count += 1
            return self._decision(False, roa_reason, route, normalized_path)

        # 5. Unreachable next hops never enter the forwarding plane.
        if not next_hop_reachable:
            self._rejected_count += 1
            return self._decision(False, "next_hop_unreachable", route,
                                  normalized_path)

        # 6. Flap damping with the configured suppression/reuse curve.
        penalty = self._decay_flap(prefix, now)
        if penalty >= self.flap_suppress:
            self._rejected_count += 1
            self._suppressed_count += 1
            return self._decision(False, "flap_suppressed", route,
                                  normalized_path)

        # 7. Max-prefix limit survives session resets.
        if (prefix not in self._seen_prefixes
                and len(self._seen_prefixes) >= self.max_prefixes):
            self._rejected_count += 1
            return self._decision(False, "max_prefixes", route,
                                  normalized_path)

        # 8. Admission: install in the RIB; blackhole routes stay out of FIB.
        communities = [str(community) for community
                       in (route.get("communities") or [])]
        is_blackhole = any(community in self.blackhole_communities
                           for community in communities)
        record = {
            "prefix": prefix,
            "origin_as": origin_as,
            "as_path": normalized_path,
            "next_hop": route.get("next_hop"),
            "peer_type": route.get("peer_type"),
            "neighbor_as": (self.parse_as(route["neighbor_as"])
                            if route.get("neighbor_as") is not None
                            else None),
            "communities": communities,
            "action": "discard" if is_blackhole else "forward",
        }
        self._routes.setdefault(prefix, []).append(record)
        self._seen_prefixes.add(prefix)
        self._sync_fib(prefix)
        self._accepted_count += 1
        return self._decision(True, "accepted", route, normalized_path,
                              action=record["action"])

    def withdraw(self, prefix, now=0):
        if prefix not in self._routes:
            return False
        # A withdraw is itself a flap event and feeds the damping curve.
        penalty = self._decay_flap(prefix, now)
        self._flap[prefix] = {"penalty": penalty + FLAP_PENALTY,
                              "updated": now}
        # Control plane and data plane are cleaned up together.
        del self._routes[prefix]
        self._fib.pop(prefix, None)
        return True

    def session_reset(self):
        # Counters and damping state intentionally persist across resets so a
        # flapping session cannot launder a prefix leak back into the table.
        self._session_resets += 1

    def best(self, prefix):
        return self._select_best(prefix)

    def rib(self):
        return {prefix: self._select_best(prefix)
                for prefix in self._routes}

    def fib(self):
        return dict(self._fib)

    def stats(self):
        return {
            "prefix_count": len(self._seen_prefixes),
            "active_prefixes": len(self._routes),
            "fib_prefixes": len(self._fib),
            "suppressed": self._suppressed_count,
            "accepted": self._accepted_count,
            "rejected": self._rejected_count,
            "session_resets": self._session_resets,
        }
