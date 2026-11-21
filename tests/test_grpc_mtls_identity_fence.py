import pytest

import grpc_mtls_identity_fence as gmif

TD = "cluster.local"
ID_A = "spiffe://cluster.local/ns/team/sa/app"
ID_B = "spiffe://cluster.local/ns/other/sa/app"


def fence():
    guard = gmif.MtlsIdentityFence(TD)
    guard.on_rotation(1, {"s1", "s2"})
    return guard


def peer(**over):
    data = {
        "spiffe_id": ID_A,
        "sans": [ID_A],
        "serial": "s1",
        "mtls": True,
        "jwt_spiffe": None,
        "chain": [],
        "expires_at": 1000,
    }
    data.update(over)
    return data


def test_spiffe_id_must_match_the_full_path():
    guard = fence()
    result = guard.check_peer(peer(spiffe_id="spiffe://cluster.local/ns/team/sa/app/extra"))
    assert result["ok"] is False
    assert result["reason"] == "invalid_spiffe"
    assert guard.check_peer(peer())["ok"] is True


def test_trust_domain_mismatch_is_rejected():
    result = fence().check_peer(peer(
        spiffe_id="spiffe://other.local/ns/team/sa/app",
        sans=["spiffe://other.local/ns/team/sa/app"],
    ))
    assert result["ok"] is False
    assert result["reason"] == "trust_domain"


def test_every_san_is_validated():
    guard = fence()
    mixed = guard.check_peer(peer(sans=[ID_A, "spiffe://other.local/ns/team/sa/app"]))
    empty = guard.check_peer(peer(sans=[]))
    assert mixed["ok"] is False
    assert mixed["reason"] in ("invalid_san", "san_mismatch", "trust_domain")
    assert empty["ok"] is False
    assert empty["reason"] == "missing_san"


def test_rotation_rejects_old_serial_immediately():
    guard = fence()
    assert guard.check_peer(peer())["ok"] is True
    guard.on_rotation(2, {"s2"})
    stale = guard.check_peer(peer())
    assert stale["ok"] is False
    assert stale["reason"] == "stale_cert"
    assert guard.check_peer(peer(serial="s2"))["ok"] is True


def test_revoked_serial_is_rejected():
    guard = fence()
    guard.revoke("s1")
    result = guard.check_peer(peer())
    assert result["ok"] is False
    assert result["reason"] == "revoked"


def test_connection_pool_is_keyed_by_identity():
    guard = fence()
    first = guard.bind_connection("conn-1", ID_A)
    second = guard.bind_connection("conn-2", ID_B)
    assert first["pool_key"] != second["pool_key"]
    with pytest.raises(ValueError):
        guard.bind_connection("conn-1", ID_B)


def test_plaintext_downgrade_is_rejected():
    guard = fence()
    result = guard.check_peer(peer(mtls=False))
    assert result["ok"] is False
    assert result["reason"] == "downgrade"
    assert guard.check_peer(None)["ok"] is False


def test_jwt_and_certificate_conflict_is_rejected():
    result = fence().check_peer(peer(jwt_spiffe=ID_B))
    assert result["ok"] is False
    assert result["reason"] == "jwt_conflict"


def test_policy_matches_method_path_and_identity():
    guard = fence()
    policy = [{"methods": ["GET"], "paths": ["/api/*"], "identities": [ID_A]}]
    assert guard.authorize(ID_A, policy, "GET", "/api/users") is True
    assert guard.authorize(ID_A, policy, "POST", "/api/users") is False
    assert guard.authorize(ID_A, policy, "GET", "/admin") is False


def test_cross_namespace_and_empty_policy_default_deny():
    guard = fence()
    policy = [{"methods": ["GET"], "paths": ["/api/*"], "identities": [ID_A]}]
    assert guard.authorize(ID_B, policy, "GET", "/api/users") is False
    assert guard.authorize(ID_A, [], "GET", "/api/users") is False


def test_rotation_invalidates_peer_cache():
    guard = fence()
    assert guard.check_peer(peer())["ok"] is True
    assert guard.state_dump()["cache_size"] == 1
    guard.on_rotation(2, {"s2"})
    assert guard.state_dump()["cache_size"] == 0
    assert guard.check_peer(peer())["ok"] is False


def test_both_allow_and_deny_decisions_are_audited():
    guard = fence()
    guard.check_peer(peer(serial="missing"))
    denied = guard.state_dump()["audit"][-1]
    guard.check_peer(peer())
    allowed = guard.state_dump()["audit"][-1]
    assert denied["decision"] == "deny"
    assert denied["reason"] == "stale_cert"
    assert allowed["decision"] == "allow"
    assert [entry["seq"] for entry in guard.state_dump()["audit"]] == [1, 2]


def test_intermediate_ca_basic_constraints_are_enforced():
    guard = fence()
    bad = guard.check_peer(peer(chain=[{"subject": "intermediate", "ca": False}]))
    good = guard.check_peer(peer(chain=[{"subject": "root", "ca": True}]))
    assert bad["ok"] is False
    assert bad["reason"] == "bad_chain"
    assert good["ok"] is True


def test_malformed_peer_is_rejected():
    guard = fence()
    assert guard.check_peer(peer(spiffe_id=None))["ok"] is False
    assert guard.check_peer(peer(sans="not-a-list"))["ok"] is False
    assert guard.check_peer(peer(serial=None))["ok"] is False


def test_state_dump_reports_counters_and_isolated_audit():
    guard = fence()
    guard.check_peer(peer())
    guard.check_peer(peer(serial="unknown"))
    info = guard.state_dump()
    assert info["generation"] == 1
    assert info["checked"] == 2
    assert info["allowed"] == 1
    assert info["denied"] == 1
    info["audit"][0]["decision"] = "tampered"
    assert guard.state_dump()["audit"][0]["decision"] == "allow"
