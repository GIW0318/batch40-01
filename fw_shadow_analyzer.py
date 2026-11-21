import ipaddress
from functools import lru_cache


CONTAINS = "contains"
WITHIN = "within"
EQUAL = "equal"
DISJOINT = "disjoint"
OVERLAP = "overlap"

SHADOWED = "shadowed"
PARTIAL = "partial"
REDUNDANT = "redundant"
RELATED = "related"

REASON_FULL_SHADOW = "full_shadow"
REASON_PARTIAL_OVERLAP = "partial_overlap"
REASON_DUPLICATE = "duplicate"
REASON_DEFAULT_POLICY = "default_policy"
REASON_DISJOINT_PORTS = "same_selector_disjoint_ports"

PROTOCOL_NUMBERS = {
    1: "icmp",
    2: "igmp",
    6: "tcp",
    17: "udp",
    47: "gre",
    50: "esp",
    51: "ah",
    58: "icmpv6",
    89: "ospf",
    132: "sctp",
}


@lru_cache(maxsize=None)
def _network(value):
    return ipaddress.ip_network(value, strict=False)


def _is_any_network(value):
    try:
        return _network(value).prefixlen == 0
    except ValueError:
        return False


class RuleShadowAnalyzer:
    def __init__(self, default_policy="deny"):
        self.default_policy = default_policy
        self._rules = []

    def add_rule(self, rule):
        normalized = self.normalize(rule)
        self._rules.append(normalized)
        return normalized

    def normalize(self, rule):
        normalized = dict(rule)
        normalized["proto"] = self._normalize_proto(rule.get("proto", "any"))
        normalized["ports"] = self._normalize_ports(rule.get("ports"))
        normalized["action"] = rule.get("action", "allow")
        normalized["state"] = rule.get("state", "any")
        normalized["nat"] = rule.get("nat", "pre")
        normalized["icmp_type"] = rule.get("icmp_type")
        normalized["icmp_code"] = rule.get("icmp_code")
        normalized["negate_src"] = bool(rule.get("negate_src", False))
        normalized["negate_dst"] = bool(rule.get("negate_dst", False))
        return normalized

    @staticmethod
    def _normalize_proto(proto):
        if isinstance(proto, int):
            return PROTOCOL_NUMBERS.get(proto, str(proto))
        if isinstance(proto, str) and proto.isdigit():
            number = int(proto)
            return PROTOCOL_NUMBERS.get(number, str(number))
        return str(proto).lower()

    @staticmethod
    def _normalize_ports(ports):
        if ports is None:
            return None
        if isinstance(ports, int):
            return (ports, ports)
        port_list = list(ports)
        if len(port_list) == 1:
            return (port_list[0], port_list[0])
        return (port_list[0], port_list[1])

    def analyze(self):
        findings = []
        covered_ids = set()

        for index, later in enumerate(self._rules):
            for earlier in self._rules[:index]:
                relation = self._pair_relation(earlier, later)
                if relation is None:
                    continue
                kind, reason = relation
                findings.append({
                    "kind": kind,
                    "rule": later["id"],
                    "by": earlier["id"],
                    "reason": reason,
                    "nat": later["nat"],
                })
                covered_ids.add(later["id"])
                break

        default_redundant = []
        for rule in self._rules:
            if rule["id"] in covered_ids:
                continue
            if self._is_redundant_with_default(rule):
                default_redundant.append(rule["id"])
                findings.append({
                    "kind": REDUNDANT,
                    "rule": rule["id"],
                    "by": "__default__",
                    "reason": REASON_DEFAULT_POLICY,
                    "nat": rule["nat"],
                })

        return {
            "default": {
                "policy": self.default_policy,
                "redundant": default_redundant,
            },
            "findings": findings,
        }

    def _is_redundant_with_default(self, rule):
        if rule["action"] != self.default_policy:
            return False
        if rule["proto"] != "any" or rule["ports"] is not None:
            return False
        if rule["state"] != "any":
            return False
        if rule["icmp_type"] is not None or rule["icmp_code"] is not None:
            return False
        if rule["negate_src"] or rule["negate_dst"]:
            return False
        return (_is_any_network(rule["src"])
                and _is_any_network(rule["dst"]))

    def _pair_relation(self, earlier, later):
        if earlier["nat"] != later["nat"]:
            return None

        relations = [
            self._proto_relation(earlier["proto"], later["proto"]),
            self._ports_relation(earlier["ports"], later["ports"]),
            self._network_relation(
                earlier["src"], earlier["negate_src"],
                later["src"], later["negate_src"],
            ),
            self._network_relation(
                earlier["dst"], earlier["negate_dst"],
                later["dst"], later["negate_dst"],
            ),
            self._wildcard_relation(earlier["state"], later["state"], "any"),
            self._wildcard_relation(
                earlier["icmp_type"], later["icmp_type"], None),
            self._wildcard_relation(
                earlier["icmp_code"], later["icmp_code"], None),
        ]

        if DISJOINT in relations:
            selector_relations = (
                relations[0], relations[2], relations[3],
                relations[4], relations[5], relations[6],
            )
            if (relations[1] == DISJOINT
                    and all(rel == EQUAL for rel in selector_relations)):
                return RELATED, REASON_DISJOINT_PORTS
            return None

        if all(rel == EQUAL for rel in relations):
            if earlier["action"] == later["action"]:
                return REDUNDANT, REASON_DUPLICATE
            return SHADOWED, REASON_FULL_SHADOW

        if all(rel in (CONTAINS, EQUAL) for rel in relations):
            return SHADOWED, REASON_FULL_SHADOW

        return PARTIAL, REASON_PARTIAL_OVERLAP

    @staticmethod
    def _proto_relation(earlier, later):
        if earlier == "any":
            return EQUAL if later == "any" else CONTAINS
        if later == "any":
            return WITHIN
        return EQUAL if earlier == later else DISJOINT

    @staticmethod
    def _ports_relation(earlier, later):
        if earlier is None:
            return EQUAL if later is None else CONTAINS
        if later is None:
            return WITHIN
        early_low, early_high = earlier
        late_low, late_high = later
        if earlier == later:
            return EQUAL
        if early_low <= late_low and early_high >= late_high:
            return CONTAINS
        if early_low >= late_low and early_high <= late_high:
            return WITHIN
        if early_high < late_low or late_high < early_low:
            return DISJOINT
        return OVERLAP

    @staticmethod
    def _network_relation(net_a, negate_a, net_b, negate_b):
        try:
            plain = RuleShadowAnalyzer._plain_network_relation(net_a, net_b)
        except (ValueError, TypeError):
            return DISJOINT

        if not negate_a and not negate_b:
            return plain
        if negate_a and not negate_b:
            if plain in (EQUAL, CONTAINS):
                return DISJOINT
            if plain == DISJOINT:
                return CONTAINS
            return OVERLAP
        if not negate_a and negate_b:
            if plain in (EQUAL, WITHIN):
                return DISJOINT
            if plain == DISJOINT:
                return WITHIN
            return OVERLAP

        if plain == EQUAL:
            return EQUAL
        if plain == CONTAINS:
            return WITHIN
        if plain == WITHIN:
            return CONTAINS
        return OVERLAP

    @staticmethod
    def _plain_network_relation(net_a, net_b):
        network_a = _network(net_a)
        network_b = _network(net_b)
        if network_a == network_b:
            return EQUAL
        if network_b.subnet_of(network_a):
            return CONTAINS
        if network_a.subnet_of(network_b):
            return WITHIN
        return DISJOINT

    @staticmethod
    def _wildcard_relation(earlier, later, wildcard):
        if earlier == wildcard:
            return EQUAL if later == wildcard else CONTAINS
        if later == wildcard:
            return WITHIN
        return EQUAL if earlier == later else DISJOINT
