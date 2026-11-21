import copy

import pytest

import split_tunnel_policy as stp

TUN = "tun0"
ETH = "eth0"

BASE_POLICY = {
    "version": 1,
    "includes": ["10.0.0.0/8", "10.1.2.0/24", "2001:db8::/32"],
    "excludes": ["10.1.0.0/16", "10.2.0.0/24", "2001:db8:bad::/48"],
    "local_networks": ["192.168.0.0/16"],
    "apps": [{"signature": "acme-vpn", "action": "bypass"}],
    "metric": 5,
    "dns": {
        "corp.example.com": {"servers": ["10.0.0.53"], "scope": "tunnel"},
        "example.com": {"servers": ["93.184.216.34"], "scope": "physical"},
    },
    "dns_records": {
        "db.corp.example.com": "10.0.0.7",
        "www.example.com": "93.184.216.34",
    },
}


def policy(**overrides):
    data = copy.deepcopy(BASE_POLICY)
    data.update(overrides)
    return data


def gate(**overrides):
    engine = stp.SplitTunnelPolicy()
    engine.load_policy(policy(**overrides), now=100)
    engine.apply(now=100)
    return engine


def test_longest_prefix_include_beats_broader_exclude():
    result = gate().decide("10.1.2.9")
    assert result["in_tunnel"] is True
    assert result["reason"] == "include"
    assert result["matched"]["cidr"] == "10.1.2.0/24"


def test_longest_prefix_exclude_beats_broader_include():
    result = gate().decide("10.1.3.4")
    assert result["in_tunnel"] is False
    assert result["reason"] == "exclude"
    assert result["matched"]["cidr"] == "10.1.0.0/16"


def test_equal_prefix_exclude_wins_tie():
    engine = gate(includes=BASE_POLICY["includes"] + ["10.2.0.0/24"])
    result = engine.decide("10.2.0.5")
    assert result["in_tunnel"] is False
    assert result["reason"] == "exclude"


def test_ipv4_mapped_ipv6_is_normalized_before_matching():
    result = gate().decide("::ffff:10.1.3.4")
    assert result["normalized"] == "10.1.3.4"
    assert result["in_tunnel"] is False
    assert result["reason"] == "exclude"


def test_ipv6_routes_and_exclusions_are_generated():
    engine = gate()
    direct = engine.decide("2001:db8:bad::9")
    tunnel = engine.decide("2001:db8:1::9")
    assert direct["in_tunnel"] is False
    assert tunnel["in_tunnel"] is True
    routes = engine.routes()
    included = [item for item in routes if item["cidr"] == "2001:db8::/32"]
    excluded = [item for item in routes if item["cidr"] == "2001:db8:bad::/48"]
    assert included and included[0]["family"] == "ipv6"
    assert included[0]["interface"] == TUN
    assert excluded and excluded[0]["interface"] == ETH


def test_local_network_is_explicitly_direct():
    result = gate().decide("192.168.1.50")
    assert result["in_tunnel"] is False
    assert result["reason"] == "local"


def test_app_identity_uses_signature_not_path():
    engine = gate()
    bypass = engine.decide("10.0.0.5", app={
        "path": "C:\\temp\\renamed-tool.exe",
        "signature": "acme-vpn",
    })
    assert bypass["in_tunnel"] is False
    assert bypass["reason"] == "app_bypass"
    inside = engine.decide("10.0.0.5", app={
        "path": "C:\\apps\\acme-vpn.exe",
        "signature": "other-vendor",
    })
    assert inside["in_tunnel"] is True


def test_internal_dns_is_only_resolved_on_tunnel_interface():
    engine = gate()
    internal = engine.resolve("db.corp.example.com", TUN, now=200)
    assert internal["resolved"] is True
    assert internal["address"] == "10.0.0.7"
    leaked = engine.resolve("db.corp.example.com", ETH, now=200)
    assert leaked["resolved"] is False
    assert leaked["reason"] == "split_dns"
    assert "address" not in leaked


def test_dns_cache_is_partitioned_per_interface():
    engine = gate()
    public = engine.resolve("www.example.com", ETH, now=200)
    assert public["resolved"] is True
    assert public["address"] == "93.184.216.34"
    cross = engine.resolve("www.example.com", TUN, now=200)
    assert cross["resolved"] is False
    cache = engine.snapshot()["dns_cache"]
    assert "www.example.com" in cache[ETH]
    assert "www.example.com" not in cache[TUN]


def test_dhcp_metric_cannot_override_policy_metric():
    engine = gate()
    engine.learn_dhcp_route(ETH, "10.0.0.0/8", 1)
    routes = engine.routes()
    matches = [item for item in routes if item["cidr"] == "10.0.0.0/8"]
    assert any(item["interface"] == TUN and item["metric"] == 5
               for item in matches)
    assert all(item["metric"] != 1 for item in routes)
    assert engine.snapshot()["dhcp_ignored"] == 1
    assert engine.decide("10.0.0.8")["in_tunnel"] is True


def test_interface_switch_fails_closed():
    engine = gate()
    signal = engine.switch_interface(ETH, "eth1", now=200)
    assert signal["blocked"] is True
    blocked = engine.decide("10.0.0.8")
    assert blocked["blocked"] is True
    assert blocked["in_tunnel"] is False
    assert blocked["reason"] == "fail_closed"
    engine.complete_switch(now=201)
    restored = engine.decide("10.0.0.8")
    assert restored["blocked"] is False
    assert restored["in_tunnel"] is True


def test_reconnect_clears_stale_routes():
    engine = stp.SplitTunnelPolicy()
    old = policy(version=1, includes=BASE_POLICY["includes"] + ["10.9.0.0/16"])
    engine.load_policy(old, now=100)
    engine.apply(now=100)
    assert any(item["cidr"] == "10.9.0.0/16" for item in engine.routes())
    engine.load_policy(policy(version=2), now=200)
    assert any(item["cidr"] == "10.9.0.0/16" for item in engine.routes())
    info = engine.reconnect(now=300)
    assert info["cleared"] >= 1
    assert not any(item["cidr"] == "10.9.0.0/16" for item in engine.routes())
    assert engine.snapshot()["stale_routes"] == 0


def test_invalid_policy_update_keeps_previous_rules_atomically():
    engine = gate()
    before = engine.routes()
    bad = policy(version=9, excludes=[123])
    with pytest.raises(ValueError):
        engine.load_policy(bad, now=200)
    assert engine.snapshot()["version"] == 1
    assert engine.routes() == before


def test_policy_version_and_effective_time_are_queryable():
    engine = stp.SplitTunnelPolicy()
    loaded = engine.load_policy(policy(version=4), now=123)
    assert loaded["version"] == 4
    assert loaded["effective_at"] == 123
    engine.apply(now=124)
    info = engine.snapshot()
    assert info["version"] == 4
    assert info["effective_at"] == 124


def test_snapshot_exports_routes_and_dns_state():
    engine = gate()
    engine.resolve("db.corp.example.com", TUN, now=200)
    info = engine.snapshot()
    assert {"routes", "dns_cache", "version", "effective_at",
            "stale_routes"} <= set(info)
    assert isinstance(info["routes"], list) and info["routes"]
    assert isinstance(info["dns_cache"], dict)
    assert "db.corp.example.com" in info["dns_cache"][TUN]
