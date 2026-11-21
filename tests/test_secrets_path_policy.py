import pytest

import secrets_path_policy as spp


RULES = [
    {
        "effect": "allow",
        "subjects": ["svc-app"],
        "actions": ["read"],
        "path": "secret/data/app/*",
    }
]


def policy(rules=None, **kwargs):
    return spp.SecretsPathPolicy(RULES if rules is None else rules, **kwargs)


def test_allowed_read_normalizes_duplicate_separators():
    result = policy().authorize("svc-app", "read", "secret//data/app/token")
    assert result["allowed"] is True
    assert result["reason"] == "allowed"
    assert result["rule"] == 0
    assert result["path"] == "secret/data/app/token"


def test_star_does_not_cross_a_path_separator():
    result = policy().authorize(
        "svc-app", "read", "secret/data/app/db/password"
    )
    assert result["allowed"] is False
    assert result["reason"] == "no_match"


def test_deny_beats_allow_regardless_of_rule_order():
    allow = {
        "effect": "allow",
        "subjects": ["svc-app"],
        "actions": ["read"],
        "path": "secret/data/*/*",
    }
    deny = {
        "effect": "deny",
        "subjects": ["svc-app"],
        "actions": ["read"],
        "path": "secret/data/app/*",
    }
    assert policy([allow, deny]).authorize(
        "svc-app", "read", "secret/data/app/token"
    )["allowed"] is False
    assert policy([deny, allow]).authorize(
        "svc-app", "read", "secret/data/app/token"
    )["allowed"] is False


def test_parent_segments_are_resolved_before_matching():
    deny = {
        "effect": "deny",
        "subjects": ["svc-app"],
        "actions": ["read"],
        "path": "secret/data/other/*",
    }
    result = policy([deny]).authorize(
        "svc-app", "read", "secret/data/app/../other/db"
    )
    assert result["allowed"] is False
    assert result["reason"] == "denied"
    assert result["path"] == "secret/data/other/db"


def test_percent_escapes_are_decoded_exactly_once():
    exact = {
        "effect": "allow",
        "subjects": ["svc-app"],
        "actions": ["read"],
        "path": "secret/data/app/db",
    }
    evaluator = policy([exact])
    assert evaluator.authorize(
        "svc-app", "read", "secret/data/app%2Fdb"
    )["allowed"] is True
    assert evaluator.authorize(
        "svc-app", "read", "secret/data/app%252Fdb"
    )["allowed"] is False


def test_path_escaping_the_root_is_rejected():
    result = policy().authorize("svc-app", "read", "../../etc/passwd")
    assert result["allowed"] is False
    assert result["reason"] == "bad_path"


def test_template_variables_are_expanded():
    rules = [
        {
            "effect": "allow",
            "subjects": ["svc-${app}"],
            "actions": ["read"],
            "path": "secret/data/${app}/*",
        }
    ]
    evaluator = policy(rules, variables={"app": "billing"})
    result = evaluator.authorize(
        "svc-billing", "read", "secret/data/billing/token"
    )
    assert result["allowed"] is True


def test_missing_template_variable_defaults_to_deny():
    rules = [
        {
            "effect": "allow",
            "subjects": ["svc-${app}"],
            "actions": ["read"],
            "path": "secret/data/${app}/*",
        }
    ]
    result = policy(rules).authorize(
        "svc-billing", "read", "secret/data/billing/token"
    )
    assert result["allowed"] is False
    assert result["reason"] == "no_match"


def test_list_permission_is_separate_from_read():
    evaluator = policy()
    assert evaluator.authorize(
        "svc-app", "read", "secret/data/app/token"
    )["allowed"] is True
    result = evaluator.authorize("svc-app", "list", "secret/data/app/token")
    assert result["allowed"] is False
    assert result["reason"] == "no_match"


def test_root_path_is_limited_separately():
    rules = [
        {
            "effect": "allow",
            "subjects": ["svc-app"],
            "actions": ["read"],
            "path": "*",
        }
    ]
    result = policy(rules).authorize("svc-app", "read", "/")
    assert result["allowed"] is False
    assert result["reason"] == "root_path"


def test_matching_is_case_sensitive_by_default():
    result = policy().authorize(
        "svc-app", "read", "Secret/Data/App/token"
    )
    assert result["allowed"] is False
    assert result["reason"] == "no_match"


def test_case_insensitive_policy_matches_any_case():
    result = policy(case_sensitive=False).authorize(
        "svc-app", "read", "Secret/Data/App/token"
    )
    assert result["allowed"] is True


def test_revoked_lease_is_denied():
    evaluator = policy()
    lease = evaluator.issue_lease("svc-app", ["read"], ["secret/data/app/*"])
    assert evaluator.authorize(
        "svc-app", "read", "secret/data/app/token", lease_id=lease
    )["allowed"] is True
    evaluator.revoke_lease(lease)
    result = evaluator.authorize(
        "svc-app", "read", "secret/data/app/token", lease_id=lease
    )
    assert result["allowed"] is False
    assert result["reason"] == "lease_revoked"


def test_revocation_cascades_to_derived_credentials():
    evaluator = policy()
    lease = evaluator.issue_lease("svc-app", ["read"], ["secret/data/app/*"])
    credential = evaluator.derive_credential(lease)
    assert evaluator.authorize(
        "svc-app", "read", "secret/data/app/token", credential=credential
    )["allowed"] is True
    evaluator.revoke_lease(lease)
    result = evaluator.authorize(
        "svc-app", "read", "secret/data/app/token", credential=credential
    )
    assert result["allowed"] is False
    assert result["reason"] == "lease_revoked"


def test_snapshot_hides_internal_rule_objects():
    evaluator = policy()
    snapshot = evaluator.snapshot()
    assert set(snapshot) == {"rule_count", "digest", "case_sensitive"}
    assert snapshot["rule_count"] == 1
    assert snapshot["case_sensitive"] is True
    assert "rules" not in snapshot
    assert len(snapshot["digest"]) == 64
    assert snapshot["digest"] == policy().snapshot()["digest"]
