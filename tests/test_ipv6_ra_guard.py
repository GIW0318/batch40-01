import ipv6_ra_guard as rag


def prefix(addr="2001:db8:1::/64", valid=2592000, preferred=604800,
           autonomous=True):
    return {
        "prefix": addr,
        "valid_lifetime": valid,
        "preferred_lifetime": preferred,
        "autonomous": autonomous,
        "on_link": True,
    }


def ra(**kw):
    base = {
        "src": "fe80::1",
        "port": "Gi0/1",
        "hop_limit": 255,
        "router_lifetime": 1800,
        "router_preference": "medium",
        "flags": {"managed": False, "other": False},
        "prefixes": [prefix()],
    }
    base.update(kw)
    return base


def guard(**kw):
    kw.setdefault("trusted_ports", {"Gi0/1"})
    kw.setdefault("dhcpv6_mode", "stateful")
    kw.setdefault("rate_limit", 10)
    kw.setdefault("rate_window", 10)
    return rag.RaPolicyGuard(**kw)


def test_non_link_local_source_is_rejected():
    result = guard().inspect(ra(src="2001:db8::1"))
    assert result["accepted"] is False
    assert result["reason"] == "non_link_local"


def test_valid_lifetime_below_preferred_is_rejected():
    result = guard().inspect(
        ra(prefixes=[prefix(valid=100, preferred=200)])
    )
    assert result["accepted"] is False
    assert result["reason"] == "invalid_lifetimes"


def test_high_router_preference_is_rejected_by_policy():
    result = guard().inspect(ra(router_preference="high"))
    assert result["accepted"] is False
    assert result["reason"] == "preference_policy"


def test_managed_flag_conflict_with_dhcpv6_mode():
    policy = guard(dhcpv6_mode="none")
    result = policy.inspect(ra(flags={"managed": True, "other": False}))
    assert result["accepted"] is False
    assert result["reason"] == "flag_conflict"


def test_managed_flag_consistent_with_dhcpv6_mode():
    policy = guard(dhcpv6_mode="stateful")
    result = policy.inspect(ra(flags={"managed": True, "other": True}))
    assert result["accepted"] is True
    assert result["reason"] == "accepted"


def test_untrusted_access_port_is_rejected():
    result = guard().inspect(ra(port="Gi0/9"))
    assert result["accepted"] is False
    assert result["reason"] == "untrusted_port"


def test_hop_limit_must_be_255():
    result = guard().inspect(ra(hop_limit=64))
    assert result["accepted"] is False
    assert result["reason"] == "bad_hop_limit"


def test_non_64_prefix_is_rejected_for_slaac():
    result = guard().inspect(
        ra(prefixes=[prefix(addr="2001:db8:1::/56")])
    )
    assert result["accepted"] is False
    assert result["reason"] == "bad_prefix_length"


def test_rate_limit_is_per_source():
    policy = guard(rate_limit=2, rate_window=10)
    first = policy.inspect(ra(src="fe80::1"), now=0)
    second = policy.inspect(ra(src="fe80::1"), now=0)
    third = policy.inspect(ra(src="fe80::1"), now=0)
    assert first["accepted"] is True
    assert second["accepted"] is True
    assert third["accepted"] is False
    assert third["reason"] == "rate_limited"
    other = policy.inspect(ra(src="fe80::2"), now=0)
    assert other["accepted"] is True


def test_router_lifetime_zero_withdraws_default_route():
    policy = guard()
    policy.inspect(ra(router_lifetime=1800), now=0)
    assert "fe80::1" in policy.default_routers()
    result = policy.inspect(ra(router_lifetime=0), now=1)
    assert result["accepted"] is True
    assert result["action"] == "withdraw_router"
    assert "fe80::1" not in policy.default_routers()


def test_withdrawal_emits_prefix_invalidation_notice():
    policy = guard()
    policy.inspect(ra(), now=0)
    policy.inspect(ra(router_lifetime=0), now=1)
    notices = policy.notices()
    assert any(
        item["prefix"] == "2001:db8:1::/64"
        and item["action"] == "invalidate"
        for item in notices
    )


def test_duplicate_prefix_is_applied_once():
    policy = guard()
    result = policy.inspect(ra(prefixes=[prefix(), prefix()]))
    assert result["accepted"] is True
    assert len(policy.prefixes()) == 1


def test_neighbor_advertisement_dad_conflict_detected():
    policy = guard()
    conflict = policy.neighbor_advertisement(
        {
            "src": "fe80::2",
            "target": "2001:db8:1::10",
            "tentative": True,
        }
    )
    assert conflict["conflict"] is True
    assert conflict["reason"] == "dad_conflict"
    clean = policy.neighbor_advertisement(
        {
            "src": "fe80::2",
            "target": "2001:db8:1::11",
            "tentative": False,
        }
    )
    assert clean["conflict"] is False


def test_rejected_decision_has_readable_reason():
    result = guard().inspect(ra(hop_limit=1))
    assert result["accepted"] is False
    assert result["reason"] == "bad_hop_limit"
    assert isinstance(result["reason"], str)


def test_accepted_ra_reports_and_tracks_prefix():
    policy = guard()
    result = policy.inspect(ra())
    assert result["accepted"] is True
    assert result["reason"] == "accepted"
    assert "2001:db8:1::/64" in policy.prefixes()
