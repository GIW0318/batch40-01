import pytest

import scope_attenuation_plan as sap

HIERARCHY = {"repo:admin": ["repo:write"], "repo:write": ["repo:read"]}
ALIASES = {"mail": ["mail:read", "mail:write"]}
KNOWN = [
    "repo:read", "repo:write", "repo:admin",
    "mail:read", "mail:write", "reports:read",
]


def plan(**kwargs):
    return sap.ScopeAttenuationPlan(
        hierarchy=HIERARCHY,
        aliases=ALIASES,
        known_scopes=KNOWN,
        **kwargs
    )


def test_refresh_cannot_regain_attenuated_parent_scope():
    p = plan()
    token = p.issue("alice", ["repo:write"], ["reports"])
    child = p.refresh(token, ["repo:read"])
    assert child["scopes"] == ["repo:read"]
    with pytest.raises(ValueError):
        p.refresh(child, ["repo:write"])


def test_refresh_with_subset_returns_new_token():
    p = plan()
    token = p.issue("alice", ["mail:read", "mail:write", "repo:read"], ["reports"])
    child = p.refresh(token, ["mail:read"])
    assert child["scopes"] == ["mail:read"]
    assert child["audiences"] == ["reports"]
    assert child["token_id"] != token["token_id"]


def test_refresh_without_requested_scopes_keeps_current_set():
    p = plan()
    token = p.issue("alice", ["repo:write"], ["reports"])
    child = p.refresh(token)
    assert child["scopes"] == ["repo:read", "repo:write"]
    assert child["token_id"] != token["token_id"]


def test_unknown_scope_on_issue_is_rejected():
    p = plan()
    with pytest.raises(ValueError):
        p.issue("alice", ["repo:delete"], ["reports"])


def test_unknown_scope_on_refresh_is_rejected():
    p = plan()
    token = p.issue("alice", ["repo:read"], ["reports"])
    with pytest.raises(ValueError):
        p.refresh(token, ["repo:destroy"])


def test_audience_must_be_explicit_and_binding():
    p = plan()
    with pytest.raises(ValueError):
        p.issue("alice", ["repo:read"], [])
    token = p.issue("alice", ["repo:read"], ["reports"])
    denied = p.check(token, "settlement")
    assert denied["allowed"] is False
    assert denied["reason"] == "audience mismatch"
    assert p.check(token, "reports")["allowed"] is True


def test_scope_case_and_whitespace_are_normalized():
    p = plan()
    token = p.issue("alice", ["Repo:Read", " repo:read "], ["reports"])
    assert token["scopes"] == ["repo:read"]
    assert len(token["scopes"]) == 1


def test_alias_is_expanded_before_comparison():
    p = plan()
    aliased = p.issue("alice", ["Mail"], ["reports"])
    explicit = p.issue("alice", ["mail:read", "mail:write"], ["reports"])
    assert aliased["scopes"] == explicit["scopes"] == ["mail:read", "mail:write"]
    spaced = p.issue("alice", [" MAIL "], ["reports"])
    assert spaced["scopes"] == aliased["scopes"]


def test_hierarchy_expands_downward_only():
    p = plan()
    token = p.issue("alice", ["repo:admin"], ["reports"])
    assert token["scopes"] == ["repo:admin", "repo:read", "repo:write"]
    child = p.refresh(token, ["repo:read"])
    assert child["scopes"] == ["repo:read"]


def test_empty_scope_list_grants_nothing():
    p = plan()
    token = p.issue("alice", [], ["reports"])
    assert token["scopes"] == []
    assert p.check(token, "reports")["allowed"] is True
    denied = p.check(token, "reports", scope="repo:read")
    assert denied["allowed"] is False
    assert denied["reason"] == "missing scope"


def test_scope_change_invalidates_old_tokens():
    p = plan()
    token = p.issue("alice", ["mail:read", "mail:write"], ["reports"])
    p.update_user_scopes("alice", ["mail:read"])
    result = p.check(token, "reports")
    assert result["allowed"] is False
    assert result["reason"] == "scope changed"
    with pytest.raises(ValueError):
        p.refresh(token)


def test_new_token_after_scope_change_uses_new_grant():
    p = plan()
    p.update_user_scopes("alice", ["mail:read"])
    token = p.issue("alice", ["mail:read"], ["reports"])
    assert p.check(token, "reports", scope="mail:read")["allowed"] is True
    assert p.check(token, "reports", scope="mail:write")["allowed"] is False


def test_impersonation_requires_a_delegation_grant():
    p = plan()
    with pytest.raises(ValueError):
        p.issue("alice", ["mail:read"], ["reports"], actor="mallory")
    token = p.issue("alice", ["mail:read"], ["reports"])
    with pytest.raises(ValueError):
        p.delegate(token, "mallory", ["mail:read"], ["reports"])


def test_delegation_is_bounded_by_actor_grant_and_audience():
    p = plan(delegations={("support1", "alice"): ["mail:read"]})
    token = p.issue("alice", ["mail:read", "mail:write"], ["reports"])
    delegated = p.delegate(token, "support1", ["mail:read"], ["reports"])
    assert delegated["actor_chain"] == ["support1"]
    assert delegated["scopes"] == ["mail:read"]
    assert p.check(delegated, "reports", scope="mail:read")["allowed"] is True
    with pytest.raises(ValueError):
        p.delegate(token, "support1", ["mail:write"], ["reports"])
    with pytest.raises(ValueError):
        p.delegate(token, "support1", ["mail:read"], ["settlement"])


def test_scope_count_is_capped():
    p = plan(max_scopes=3)
    token = p.issue("alice", ["repo:admin"], ["reports"])
    assert len(token["scopes"]) == 3
    with pytest.raises(ValueError):
        p.issue("alice", ["repo:admin", "mail:read"], ["reports"])
