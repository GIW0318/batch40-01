"""Split tunnel routing, split DNS and fail-closed policy engine."""

import ipaddress

_APP_ACTIONS = {"bypass", "tunnel"}
_DNS_SCOPES = {"tunnel", "physical"}


def _normalize_destination(destination):
    addr = ipaddress.ip_address(destination)
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr


def _parse_networks(policy, key):
    raw = policy.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
        raise ValueError(f"{key} must be a list of CIDR strings")
    try:
        return [ipaddress.ip_network(c) for c in raw]
    except ValueError as exc:
        raise ValueError(f"{key} contains an invalid CIDR: {exc}") from exc


def _validate_policy(policy):
    if not isinstance(policy, dict):
        raise ValueError("policy must be a mapping")
    version = policy.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("policy version must be an integer")
    metric = policy.get("metric", 0)
    if not isinstance(metric, int) or isinstance(metric, bool) or metric < 0:
        raise ValueError("policy metric must be a non-negative integer")

    apps = {}
    for rule in policy.get("apps", []):
        if (not isinstance(rule, dict)
                or not isinstance(rule.get("signature"), str)
                or rule.get("action") not in _APP_ACTIONS):
            raise ValueError("app rules need a signature and a valid action")
        apps[rule["signature"]] = rule["action"]

    dns = {}
    for suffix, cfg in policy.get("dns", {}).items():
        if (not isinstance(suffix, str) or not isinstance(cfg, dict)
                or cfg.get("scope") not in _DNS_SCOPES
                or not isinstance(cfg.get("servers"), list)):
            raise ValueError(f"invalid split DNS rule for {suffix!r}")
        dns[suffix] = {"servers": list(cfg["servers"]), "scope": cfg["scope"]}

    records = policy.get("dns_records", {})
    if (not isinstance(records, dict)
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in records.items())):
        raise ValueError("dns_records must map names to address strings")

    return {
        "version": version,
        "includes": _parse_networks(policy, "includes"),
        "excludes": _parse_networks(policy, "excludes"),
        "local": _parse_networks(policy, "local_networks"),
        "apps": apps,
        "metric": metric,
        "dns": dns,
        "dns_records": dict(records),
    }


class SplitTunnelPolicy:
    """Computes tunnel routes and DNS answers for a split tunnel client."""

    def __init__(self, tunnel_interface="tun0", physical_interface="eth0"):
        self.tunnel_interface = tunnel_interface
        self.physical_interface = physical_interface
        self._staged = None
        self._active = None
        self._routes = []
        self._dns_cache = {tunnel_interface: {}, physical_interface: {}}
        self._fail_closed = False
        self._pending_interface = None
        self._dhcp_ignored = 0
        self._effective_at = None
        self._generation = 0

    def load_policy(self, policy, now=0):
        validated = _validate_policy(policy)
        self._staged = {"validated": validated, "version": validated["version"]}
        return {"version": validated["version"], "effective_at": now}

    def apply(self, now=0):
        if self._staged is not None:
            staged = self._staged
            self._staged = None
            self._activate(staged, now)
        return {"version": self._active_version(), "effective_at": self._effective_at}

    def learn_dhcp_route(self, interface, cidr, metric):
        net = ipaddress.ip_network(cidr)
        policy_metric = self._policy_metric()
        if self._active is not None:
            for include in self._active["validated"]["includes"]:
                if net.version == include.version and net.overlaps(include):
                    self._dhcp_ignored += 1
                    return {"installed": False, "reason": "policy_override"}
        effective_metric = max(metric, policy_metric)
        self._routes.append(self._route(net, interface, effective_metric, "dhcp"))
        return {"installed": True, "metric": effective_metric}

    def decide(self, destination, app=None, now=0):
        addr = _normalize_destination(destination)
        result = {
            "destination": destination,
            "normalized": str(addr),
            "blocked": False,
        }
        if self._fail_closed:
            result.update(blocked=True, in_tunnel=False, reason="fail_closed")
            return result
        if self._active is None:
            result.update(in_tunnel=False, reason="default")
            return result
        validated = self._active["validated"]
        if app:
            action = validated["apps"].get(app.get("signature"))
            if action == "bypass":
                result.update(in_tunnel=False, reason="app_bypass")
                return result
            if action == "tunnel":
                result.update(in_tunnel=True, reason="app_tunnel")
                return result
        for net in validated["local"]:
            if addr.version == net.version and addr in net:
                result.update(in_tunnel=False, reason="local",
                              matched={"cidr": str(net), "kind": "local"})
                return result
        best = None
        for kind, networks in (("include", validated["includes"]),
                               ("exclude", validated["excludes"])):
            for net in networks:
                if addr.version != net.version or addr not in net:
                    continue
                tiebreak = 1 if kind == "exclude" else 0
                key = (net.prefixlen, tiebreak)
                if best is None or key > best[0]:
                    best = (key, kind, net)
        if best is None:
            result.update(in_tunnel=False, reason="default")
            return result
        _, kind, net = best
        result.update(in_tunnel=kind == "include", reason=kind,
                      matched={"cidr": str(net), "kind": kind})
        return result

    def resolve(self, name, interface, now=0):
        cache = self._dns_cache.setdefault(interface, {})
        if name in cache:
            return {"resolved": True, "address": cache[name],
                    "cached": True, "interface": interface}
        if self._active is None:
            return {"resolved": False, "reason": "no_policy",
                    "interface": interface}
        validated = self._active["validated"]
        scope = self._dns_scope(validated, name)
        expected = {"tunnel": self.tunnel_interface,
                    "physical": self.physical_interface}.get(scope)
        if expected is not None and interface != expected:
            return {"resolved": False, "reason": "split_dns",
                    "interface": interface}
        address = validated["dns_records"].get(name)
        if address is None:
            return {"resolved": False, "reason": "no_record",
                    "interface": interface}
        cache[name] = address
        return {"resolved": True, "address": address,
                "cached": False, "interface": interface}

    def switch_interface(self, from_interface, to_interface, now=0):
        self._fail_closed = True
        self._pending_interface = to_interface
        return {"blocked": True, "from": from_interface, "to": to_interface}

    def complete_switch(self, now=0):
        if self._pending_interface is not None:
            self.physical_interface = self._pending_interface
            self._dns_cache.setdefault(self.physical_interface, {})
            self._pending_interface = None
            if self._active is not None:
                self._activate(self._active, now)
        self._fail_closed = False
        return {"blocked": False, "interface": self.physical_interface}

    def reconnect(self, now=0):
        cleared = len(self._routes)
        self._routes = []
        policy = self._staged or self._active
        self._staged = None
        if policy is not None:
            self._activate(policy, now)
        return {"cleared": cleared, "version": self._active_version(),
                "effective_at": self._effective_at}

    def routes(self):
        return [dict(route) for route in self._routes]

    def snapshot(self):
        return {
            "version": self._active_version(),
            "effective_at": self._effective_at,
            "routes": self.routes(),
            "dns_cache": {iface: dict(entries)
                          for iface, entries in self._dns_cache.items()},
            "stale_routes": sum(1 for route in self._routes
                                if route["generation"] != self._generation),
            "dhcp_ignored": self._dhcp_ignored,
            "fail_closed": self._fail_closed,
            "tunnel_interface": self.tunnel_interface,
            "physical_interface": self.physical_interface,
        }

    def _activate(self, policy, now):
        self._active = policy
        self._generation += 1
        self._routes = self._build_routes(policy["validated"])
        self._effective_at = now

    def _build_routes(self, validated):
        metric = validated["metric"]
        routes = []
        for net in validated["includes"]:
            routes.append(self._route(net, self.tunnel_interface, metric, "include"))
        for net in validated["excludes"]:
            routes.append(self._route(net, self.physical_interface, metric, "exclude"))
        for net in validated["local"]:
            routes.append(self._route(net, self.physical_interface, metric, "local"))
        return routes

    def _route(self, net, interface, metric, kind):
        return {
            "cidr": str(net),
            "interface": interface,
            "metric": metric,
            "family": "ipv6" if net.version == 6 else "ipv4",
            "kind": kind,
            "generation": self._generation,
        }

    def _active_version(self):
        return self._active["version"] if self._active is not None else None

    def _policy_metric(self):
        if self._active is None:
            return 0
        return self._active["validated"]["metric"]

    @staticmethod
    def _dns_scope(validated, name):
        best = None
        for suffix, cfg in validated["dns"].items():
            if name == suffix or name.endswith("." + suffix):
                if best is None or len(suffix) > len(best[0]):
                    best = (suffix, cfg["scope"])
        return best[1] if best is not None else None
