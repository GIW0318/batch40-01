import bgp_aspath_leak_fence as balf


BASE = {
    "prefix": "10.1.2.0/24",
    "origin_as": 65001,
    "as_path": [65010, 65001],
    "neighbor_as": 65010,
    "peer_type": "customer",
    "learned_from": "customer",
    "family": "ipv4",
    "rpki": "valid",
    "aspa": True,
    "local_pref": 100,
    "communities": [],
}


def route(**kw):
    item = dict(BASE)
    item["as_path"] = list(BASE["as_path"])
    item["communities"] = list(BASE["communities"])
    item.update(kw)
    return item


def fence(**kw):
    kw.setdefault("allowed_prefixes", ["10.1.0.0/16"])
    return balf.AsPathLeakFence(65000, **kw)


def test_customer_route_is_exported_valley_free():
    policy = fence()
    result = policy.export_policy(
        route(learned_from="customer", peer_type="customer"), "peer"
    )
    assert result["ok"] is True
    assert result["reason"] == "accepted"


def test_peer_route_cannot_be_exported_to_peer():
    policy = fence()
    result = policy.export_policy(route(learned_from="peer"), "peer")
    assert result["ok"] is False
    assert result["reason"] == "valley_free"


def test_community_tag_cannot_override_export_policy():
    policy = fence()
    tagged = route(
        learned_from="peer",
        communities=["65000:999", "no-export"],
    )
    result = policy.export_policy(tagged, "peer")
    assert result["ok"] is False
    assert result["reason"] == "valley_free"


def test_as_set_segment_is_rejected():
    policy = fence()
    result = policy.validate(
        route(as_path=[65010, ("set", [65001, 65002])])
    )
    assert result["ok"] is False
    assert result["reason"] == "as_set"


def test_prepended_path_does_not_win_shortest_path():
    policy = fence()
    honest = route(as_path=[65010, 65001])
    prepended = route(as_path=[65010, 65010, 65010, 65001])
    assert policy.update(honest, now=0)["ok"] is True
    assert policy.update(prepended, now=0)["ok"] is True
    rib = policy.snapshot()["rib"]
    assert rib["10.1.2.0/24"]["as_path"] == [65010, 65001]


def test_rpki_invalid_is_discarded():
    policy = fence()
    result = policy.update(route(rpki="invalid"), now=0)
    assert result["ok"] is False
    assert result["reason"] == "rpki_invalid"
    assert "10.1.2.0/24" not in policy.snapshot()["rib"]


def test_invalid_route_with_high_local_pref_does_not_beat_valid_route():
    policy = fence()
    assert policy.update(route(local_pref=100), now=0)["ok"] is True
    leaked = policy.update(
        route(rpki="invalid", local_pref=500), now=0
    )
    assert leaked["ok"] is False
    best = policy.snapshot()["rib"]["10.1.2.0/24"]
    assert best["local_pref"] == 100


def test_missing_aspa_relationship_is_rejected():
    policy = fence()
    result = policy.validate(route(aspa=False))
    assert result["ok"] is False
    assert result["reason"] == "aspa_missing"


def test_prefix_length_limits_are_per_address_family():
    policy = fence(
        allowed_prefixes=["10.0.0.0/8", "2001:db8::/32"],
        max_prefix_v4=24,
        max_prefix_v6=48,
    )
    assert policy.validate(route(prefix="10.1.2.0/24"))["ok"] is True
    too_long_v4 = policy.validate(route(prefix="10.1.2.128/25"))
    assert too_long_v4["ok"] is False
    assert too_long_v4["reason"] == "prefix_too_long"
    assert policy.validate(
        route(prefix="2001:db8:1::/48", family="ipv6")
    )["ok"] is True
    too_long_v6 = policy.validate(
        route(prefix="2001:db8:1::/56", family="ipv6")
    )
    assert too_long_v6["ok"] is False
    assert too_long_v6["reason"] == "prefix_too_long"


def test_confederation_segment_is_expanded():
    policy = fence()
    result = policy.validate(
        route(
            as_path=[65010, ("confed", [65020, 65021]), 65001],
            origin_as=65001,
        )
    )
    assert result["ok"] is True
    assert result["expanded_path"] == [65010, 65020, 65021, 65001]


def test_session_reset_withdraws_neighbor_routes():
    policy = fence()
    assert policy.update(
        route(prefix="10.1.1.0/24", neighbor_as=65010), now=0
    )["ok"] is True
    assert policy.update(
        route(prefix="10.1.2.0/24", neighbor_as=65020), now=0
    )["ok"] is True
    withdrawn = policy.session_reset(65010)
    assert withdrawn == ["10.1.1.0/24"]
    rib = policy.snapshot()["rib"]
    assert "10.1.1.0/24" not in rib
    assert "10.1.2.0/24" in rib


def test_import_and_export_share_one_prefix_set():
    policy = fence(allowed_prefixes=["10.1.0.0/16"])
    allowed = route(prefix="10.1.2.0/24")
    denied = route(prefix="10.9.2.0/24")
    assert policy.import_policy(allowed)["ok"] is True
    denied_import = policy.import_policy(denied)
    assert denied_import["ok"] is False
    assert denied_import["reason"] == "prefix_not_allowed"
    assert policy.export_policy(allowed, "peer")["ok"] is True
    denied_export = policy.export_policy(denied, "peer")
    assert denied_export["ok"] is False
    assert denied_export["reason"] == "prefix_not_allowed"


def test_flap_damping_is_per_prefix():
    policy = fence(
        flap_half_life=300, flap_suppress=2000, flap_reuse=500
    )
    first = route(prefix="10.1.1.0/24")
    assert policy.update(first, now=0)["ok"] is True
    policy.withdraw("10.1.1.0/24", now=0)
    assert policy.update(first, now=0)["ok"] is True
    policy.withdraw("10.1.1.0/24", now=0)
    suppressed = policy.update(first, now=0)
    assert suppressed["ok"] is False
    assert suppressed["reason"] == "flap_suppressed"
    other = route(prefix="10.1.2.0/24")
    assert policy.update(other, now=0)["ok"] is True


def test_decisions_carry_reasons():
    policy = fence()
    accepted = policy.validate(route())
    rejected = policy.validate(route(rpki="invalid"))
    assert accepted["ok"] is True
    assert accepted["reason"] == "accepted"
    assert rejected["ok"] is False
    assert rejected["reason"] == "rpki_invalid"


def test_snapshot_reports_rib_and_stats():
    policy = fence()
    assert policy.update(route(), now=0)["ok"] is True
    snapshot = policy.snapshot()
    assert snapshot["stats"]["installed"] == 1
    assert snapshot["rib"]["10.1.2.0/24"]["neighbor_as"] == 65010
