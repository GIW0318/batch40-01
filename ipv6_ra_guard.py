import ipaddress
from collections import defaultdict, deque

_PREFERENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _is_link_local(src):
    try:
        return ipaddress.IPv6Address(src).is_link_local
    except (ipaddress.AddressValueError, ValueError, TypeError):
        return False


def _prefix_length(prefix):
    try:
        return ipaddress.IPv6Network(prefix["prefix"]).prefixlen
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError,
            ValueError, KeyError, TypeError):
        return None


class RaPolicyGuard:
    """IPv6 router advertisement policy guard with per-source state."""

    def __init__(self, trusted_ports=None, dhcpv6_mode="stateful",
                 rate_limit=3, rate_window=1, max_preference="medium"):
        self.trusted_ports = set(trusted_ports or ())
        self.dhcpv6_mode = dhcpv6_mode
        self.rate_limit = rate_limit
        self.rate_window = rate_window
        self.max_preference = max_preference
        self._routers = set()
        self._prefixes_by_src = defaultdict(set)
        self._rate_windows = defaultdict(deque)
        self._na_bindings = {}
        self._notices = []

    def inspect(self, ra, now=0):
        src = ra.get("src")
        reason = self._check(ra, src, now)
        if reason is not None:
            self._invalidate_source(src)
            return {"accepted": False, "reason": reason, "action": "drop"}

        prefixes = ra.get("prefixes") or []
        if ra.get("router_lifetime", 0) > 0:
            self._routers.add(src)
            self._prefixes_by_src[src].update(
                p["prefix"] for p in prefixes if "prefix" in p
            )
            return {"accepted": True, "reason": "accepted", "action": "apply"}

        self._routers.discard(src)
        self._invalidate_source(src)
        return {"accepted": True, "reason": "accepted",
                "action": "withdraw_router"}

    def _check(self, ra, src, now):
        if ra.get("hop_limit") != 255:
            return "bad_hop_limit"
        if not _is_link_local(src):
            return "non_link_local"
        if ra.get("port") not in self.trusted_ports:
            return "untrusted_port"
        if self._rate_limited(src, now):
            return "rate_limited"

        for prefix in ra.get("prefixes") or []:
            valid = prefix.get("valid_lifetime", 0)
            preferred = prefix.get("preferred_lifetime", 0)
            if valid < preferred:
                return "invalid_lifetimes"
            if prefix.get("autonomous") and _prefix_length(prefix) != 64:
                return "bad_prefix_length"

        rank = _PREFERENCE_RANK.get(ra.get("router_preference", "medium"), 0)
        ceiling = _PREFERENCE_RANK.get(self.max_preference, 1)
        if rank > ceiling:
            return "preference_policy"

        flags = ra.get("flags") or {}
        if flags.get("managed") and self.dhcpv6_mode != "stateful":
            return "flag_conflict"
        if flags.get("other") and self.dhcpv6_mode == "none":
            return "flag_conflict"
        return None

    def _rate_limited(self, src, now):
        window = self._rate_windows[src]
        cutoff = now - self.rate_window
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self.rate_limit:
            return True
        window.append(now)
        return False

    def _invalidate_source(self, src):
        known = self._prefixes_by_src.pop(src, None)
        if not known:
            return
        remaining = set()
        for other, owned in self._prefixes_by_src.items():
            remaining |= owned
        for name in sorted(known):
            if name not in remaining:
                self._notices.append({
                    "prefix": name,
                    "action": "invalidate",
                    "src": src,
                })

    def neighbor_advertisement(self, na):
        src = na.get("src")
        target = na.get("target")
        if na.get("tentative"):
            return {"conflict": True, "reason": "dad_conflict"}
        owner = self._na_bindings.get(target)
        if owner is not None and owner != src:
            return {"conflict": True, "reason": "dad_conflict"}
        self._na_bindings[target] = src
        return {"conflict": False, "reason": "no_conflict"}

    def default_routers(self):
        return sorted(self._routers)

    def prefixes(self):
        all_prefixes = set()
        for owned in self._prefixes_by_src.values():
            all_prefixes |= owned
        return sorted(all_prefixes)

    def notices(self):
        return list(self._notices)
