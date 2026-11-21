import pytest

import bgp_prefix_guard as bpg


ROAS = [{"prefix": "10.1.0.0/16", "max_length": 24, "origin_as": 65001}]


def route(prefix="10.1.2.0/24", origin=65001, path=None,
          next_hop="192.0.2.9", peer_type="external",
          communities=None, neighbor=65010):
    return {
        "prefix": prefix,
        "origin_as": origin,
        "as_path": list(path) if path is not None else [65010, 65001],
        "next_hop": next_hop,
        "peer_type": peer_type,
        "neighbor_as": neighbor,
        "communities": list(communities or []),
    }


def guard(**kw):
    kw.setdefault("local_as", 65000)
    kw.setdefault("roas", ROAS)
    return bpg.BgpPrefixGuard(**kw)


def test_roa_max_length_allows_boundary_and_rejects_more_specific():
    table = guard()
    accepted = table.announce(route(prefix="10.1.2.0/24"))
    assert accepted["accepted"] is True
    rejected = table.announce(route(prefix="10.1.2.128/25"))
    assert rejected["accepted"] is False
    assert rejected["reason"] == "too_specific"


def test_origin_mismatch_is_rejected_not_logged():
    table = guard()
    result = table.announce(route(origin=65002))
    assert result["accepted"] is False
    assert result["reason"] == "origin_mismatch"
    assert "10.1.2.0/24" not in table.rib()


def test_missing_roa_is_rejected():
    table = guard(roas=[])
    result = table.announce(route(prefix="192.0.2.0/24"))
    assert result["accepted"] is False
    assert result["reason"] == "no_roa"


def test_own_as_anywhere_in_path_triggers_loop():
    table = guard()
    result = table.announce(route(path=[65010, 65000, 65001]))
    assert result["accepted"] is False
    assert result["reason"] == "as_loop"


def test_as_set_path_is_rejected():
    table = guard()
    result = table.announce(route(path=[65010, ("set", [65001, 65002])]))
    assert result["accepted"] is False
    assert result["reason"] == "as_set"


def test_asdot_notation_is_parsed():
    roas = [{"prefix": "10.1.0.0/16", "max_length": 24, "origin_as": 65546}]
    table = guard(roas=roas)
    assert table.parse_as("1.10") == 65546
    assert table.parse_as("65001") == 65001
    result = table.announce(route(origin=65546, path=[65010, "1.10"]))
    assert result["accepted"] is True
    assert result["as_path"][-1] == 65546


def test_invalid_asdot_is_rejected():
    table = guard()
    with pytest.raises(ValueError):
        table.parse_as("1.70000")
    with pytest.raises(ValueError):
        table.parse_as("not-an-as")


def test_default_route_is_rejected_by_default():
    table = guard()
    result = table.announce(route(prefix="0.0.0.0/0"))
    assert result["accepted"] is False
    assert result["reason"] == "default_route"


def test_next_hop_unreachable_route_is_not_installed():
    table = guard()
    result = table.announce(route(), next_hop_reachable=False)
    assert result["accepted"] is False
    assert result["reason"] == "next_hop_unreachable"
    assert "10.1.2.0/24" not in table.fib()


def test_external_route_beats_internal():
    table = guard()
    internal = route(peer_type="internal", path=[65001], neighbor=65000)
    external = route(peer_type="external", path=[65010, 65001])
    assert table.announce(internal)["accepted"] is True
    assert table.announce(external)["accepted"] is True
    assert table.best("10.1.2.0/24")["peer_type"] == "external"
    assert table.fib()["10.1.2.0/24"]["peer_type"] == "external"


def test_withdraw_clears_rib_and_fib():
    table = guard()
    table.announce(route())
    assert "10.1.2.0/24" in table.rib()
    assert "10.1.2.0/24" in table.fib()
    assert table.withdraw("10.1.2.0/24", now=1) is True
    assert "10.1.2.0/24" not in table.rib()
    assert "10.1.2.0/24" not in table.fib()
    assert table.withdraw("10.1.2.0/24", now=2) is False


def test_max_prefix_count_survives_session_reset():
    table = guard(max_prefixes=2)
    assert table.announce(route(prefix="10.1.1.0/24"))["accepted"] is True
    assert table.announce(route(prefix="10.1.2.0/24"))["accepted"] is True
    blocked = table.announce(route(prefix="10.1.3.0/24"))
    assert blocked["accepted"] is False
    assert blocked["reason"] == "max_prefixes"
    table.session_reset()
    assert table.stats()["prefix_count"] == 2
    again = table.announce(route(prefix="10.1.3.0/24"))
    assert again["accepted"] is False
    assert again["reason"] == "max_prefixes"


def test_flap_damping_suppresses_and_recovers():
    table = guard(flap_half_life=300, flap_suppress=2000, flap_reuse=500)
    assert table.announce(route(), now=0)["accepted"] is True
    table.withdraw("10.1.2.0/24", now=0)
    assert table.announce(route(), now=0)["accepted"] is True
    table.withdraw("10.1.2.0/24", now=0)
    suppressed = table.announce(route(), now=0)
    assert suppressed["accepted"] is False
    assert suppressed["reason"] == "flap_suppressed"
    recovered = table.announce(route(), now=1200)
    assert recovered["accepted"] is True


def test_blackhole_community_is_discarded_from_fib():
    table = guard()
    result = table.announce(route(communities=["65535:666"]))
    assert result["accepted"] is True
    assert result["action"] == "discard"
    assert "10.1.2.0/24" in table.rib()
    assert "10.1.2.0/24" not in table.fib()


def test_every_decision_carries_a_reason():
    table = guard()
    accepted = table.announce(route())
    rejected = table.announce(route(prefix="10.1.2.128/25"))
    assert accepted["reason"] == "accepted"
    assert isinstance(rejected["reason"], str)
    assert rejected["reason"]
