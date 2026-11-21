import pytest

import fw_shadow_analyzer as fsa


def rule(rule_id, action="allow", src="10.0.0.0/8", dst="0.0.0.0/0",
         proto="tcp", ports=(80, 80), state="any", nat="pre",
         icmp_type=None, icmp_code=None, negate_src=False,
         negate_dst=False, comment="same"):
    return {
        "id": rule_id,
        "action": action,
        "src": src,
        "dst": dst,
        "proto": proto,
        "ports": ports,
        "state": state,
        "nat": nat,
        "icmp_type": icmp_type,
        "icmp_code": icmp_code,
        "negate_src": negate_src,
        "negate_dst": negate_dst,
        "comment": comment,
    }


def analyzer(*rules, default_policy="deny"):
    instance = fsa.RuleShadowAnalyzer(default_policy=default_policy)
    for item in rules:
        instance.add_rule(item)
    return instance


def kinds(report):
    return [finding["kind"] for finding in report["findings"]]


def test_protocol_number_and_name_are_equivalent():
    instance = analyzer(
        rule("r1", proto=6),
        rule("r2", proto="tcp"),
    )
    assert instance.normalize(rule("tmp", proto=6))["proto"] == "tcp"
    report = instance.analyze()
    assert kinds(report) == ["redundant"]
    assert report["findings"][0]["rule"] == "r2"


def test_single_port_normalizes_to_range_and_shadows():
    instance = analyzer(
        rule("r1", ports=80),
        rule("r2", action="deny", ports=80),
    )
    assert instance.normalize(rule("tmp", ports=80))["ports"] == (80, 80)
    assert kinds(instance.analyze()) == ["shadowed"]


def test_cidr_containment_is_not_string_prefix():
    disjoint = analyzer(
        rule("r1", src="10.1.0.0/16"),
        rule("r2", action="deny", src="10.10.0.0/16"),
    )
    assert disjoint.analyze()["findings"] == []
    containing = analyzer(
        rule("r1", src="10.0.0.0/8"),
        rule("r2", action="deny", src="10.10.0.0/16"),
    )
    assert kinds(containing.analyze()) == ["shadowed"]


def test_allow_any_shadows_later_deny():
    instance = analyzer(
        rule("r1", src="0.0.0.0/0", proto="any", ports=None),
        rule("r2", action="deny", src="10.0.0.0/8", proto="any", ports=None),
    )
    report = instance.analyze()
    assert kinds(report) == ["shadowed"]
    assert report["findings"][0]["by"] == "r1"


def test_port_range_containment_shadows():
    instance = analyzer(
        rule("r1", ports=(1000, 2000)),
        rule("r2", action="deny", ports=(1500, 1600)),
    )
    assert kinds(instance.analyze()) == ["shadowed"]


def test_partial_port_overlap_is_reported_as_partial():
    instance = analyzer(
        rule("r1", ports=(1000, 2000)),
        rule("r2", action="deny", ports=(1500, 2500)),
    )
    report = instance.analyze()
    assert kinds(report) == ["partial"]
    assert report["findings"][0]["reason"] == "partial_overlap"


def test_established_rule_does_not_shadow_new_connections():
    instance = analyzer(
        rule("r1", state="established"),
        rule("r2", action="deny", state="new"),
    )
    assert instance.analyze()["findings"] == []


def test_negated_source_rule_is_modeled():
    instance = analyzer(
        rule("r1", src="10.0.0.0/8", negate_src=True),
        rule("r2", action="deny", src="10.1.0.0/16"),
        rule("r3", action="deny", src="192.168.0.0/16"),
    )
    report = instance.analyze()
    assert kinds(report) == ["shadowed"]
    assert report["findings"][0]["rule"] == "r3"


def test_icmp_type_and_code_are_distinct_dimensions():
    instance = analyzer(
        rule("r1", proto="icmp", icmp_type=8),
        rule("r2", action="deny", proto="icmp", icmp_type=0),
    )
    assert instance.analyze()["findings"] == []


def test_narrower_icmp_code_is_shadowed():
    instance = analyzer(
        rule("r1", proto="icmp", icmp_type=3, icmp_code=None),
        rule("r2", action="deny", proto="icmp", icmp_type=3, icmp_code=1),
    )
    assert kinds(instance.analyze()) == ["shadowed"]


def test_nat_pre_and_post_rules_are_not_compared():
    instance = analyzer(
        rule("r1", src="0.0.0.0/0", proto="any", ports=None, nat="pre"),
        rule("r2", action="deny", src="10.0.0.0/8", proto="any", ports=None, nat="post"),
    )
    assert instance.analyze()["findings"] == []


def test_default_policy_makes_deny_all_redundant():
    instance = analyzer(
        rule("r1", action="deny", src="0.0.0.0/0", proto="any", ports=None),
    )
    report = instance.analyze()
    assert report["default"]["policy"] == "deny"
    assert report["default"]["redundant"] == ["r1"]
    assert kinds(report) == ["redundant"]
    assert report["findings"][0]["by"] == "__default__"


def test_different_comments_do_not_make_rules_distinct():
    instance = analyzer(
        rule("r1", comment="primary"),
        rule("r2", comment="legacy copy"),
    )
    report = instance.analyze()
    assert kinds(report) == ["redundant"]
    assert report["findings"][0]["reason"] == "duplicate"


def test_findings_are_deterministic_and_in_file_order():
    instance = analyzer(
        rule("r1", ports=(1000, 2000)),
        rule("r2", action="deny", ports=(1500, 1600)),
        rule("r3", action="deny", ports=(1800, 2500), state="new"),
    )
    first = instance.analyze()
    second = instance.analyze()
    assert first == second
    assert [finding["rule"] for finding in first["findings"]] == ["r2", "r3"]
    assert kinds(first) == ["shadowed", "partial"]


def test_rules_with_disjoint_ports_are_related():
    instance = analyzer(
        rule("r1", ports=(80, 80)),
        rule("r2", ports=(443, 443)),
    )
    report = instance.analyze()
    assert kinds(report) == ["related"]
    assert report["findings"][0]["reason"] == "same_selector_disjoint_ports"
