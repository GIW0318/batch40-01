import pytest

import stepup_decision_gate as sdg

POLICIES = {
    "payout.change": {"risk_threshold": 50, "min_assurance": 4, "grace_seconds": 300},
    "db.drop": {"risk_threshold": 30, "min_assurance": 4, "grace_seconds": 0},
    "settings.update": {"risk_threshold": 10, "min_assurance": 2, "grace_seconds": 0},
    "document.read": {"risk_threshold": 100, "min_assurance": 1, "grace_seconds": 0},
    "device.trust": {"risk_threshold": 100, "min_assurance": 3, "grace_seconds": 0},
}


def gate(**kwargs):
    return sdg.StepupDecisionGate(policies=POLICIES, **kwargs)


def request(user="alice", action="payout.change", resource="acct:42",
            risk=10, assurance=4, available=True, verification=None, device="laptop-1"):
    return {
        "user": user,
        "action": action,
        "resource": resource,
        "risk_score": risk,
        "risk_available": available,
        "session_assurance": assurance,
        "device": device,
        "verification": verification,
    }


def stepup_for(g, req, method="hardware", now=0):
    decision = g.decide(req, now=now)
    assert decision["decision"] == "stepup"
    verified = g.verify(decision["challenge_id"], method, now=now)
    assert verified["verified"] is True
    return verified["verification"], decision


def test_high_risk_sensitive_action_requires_stepup():
    g = gate()
    decision = g.decide(request(risk=90, assurance=1), now=0)
    assert decision["decision"] == "stepup"
    assert decision["required_assurance"] == 4
    assert decision["challenge_id"]


def test_low_risk_with_adequate_assurance_is_allowed():
    g = gate()
    decision = g.decide(request(risk=10, assurance=4), now=0)
    assert decision["decision"] == "allow"
    assert decision["reason"] == "policy"


def test_grace_allows_within_window_without_reverification():
    g = gate()
    verification, _ = stepup_for(g, request(risk=90, assurance=1))
    first = g.decide(request(risk=90, assurance=1, verification=verification), now=1)
    assert first["decision"] == "allow"
    later = g.decide(request(risk=90, assurance=1), now=100)
    assert later["decision"] == "allow"
    assert later["reason"] == "grace"


def test_grace_does_not_slide_on_repeated_use():
    g = gate()
    verification, _ = stepup_for(g, request(risk=90, assurance=1))
    g.decide(request(risk=90, assurance=1, verification=verification), now=0)
    assert g.decide(request(risk=90, assurance=1), now=250)["decision"] == "allow"
    assert g.decide(request(risk=90, assurance=1), now=400)["decision"] == "stepup"


def test_grace_is_scoped_per_action():
    g = gate()
    verification, _ = stepup_for(g, request(risk=90, assurance=1))
    g.decide(request(risk=90, assurance=1, verification=verification), now=0)
    other = g.decide(request(action="db.drop", resource="table:users",
                             risk=80, assurance=4), now=10)
    assert other["decision"] == "stepup"


def test_risk_is_recomputed_for_every_action():
    g = gate()
    first = g.decide(request(risk=10, assurance=4), now=0)
    assert first["decision"] == "allow"
    second = g.decide(request(risk=95, assurance=4), now=5)
    assert second["decision"] == "stepup"


def test_risk_service_outage_fails_closed():
    g = gate()
    verification, _ = stepup_for(g, request(risk=90, assurance=1))
    g.decide(request(risk=90, assurance=1, verification=verification), now=0)
    outage = g.decide(request(risk=90, assurance=1, available=False), now=100)
    assert outage["decision"] == "deny"
    assert outage["reason"] == "risk unavailable"


def test_verification_is_bound_to_resource_and_action():
    g = gate()
    verification, _ = stepup_for(g, request(resource="acct:42", risk=90, assurance=1))
    wrong_resource = g.decide(request(resource="acct:99", risk=90, assurance=1,
                                      verification=verification), now=1)
    assert wrong_resource["decision"] == "deny"
    wrong_action = g.decide(request(action="db.drop", resource="table:users",
                                    risk=90, assurance=1, verification=verification), now=1)
    assert wrong_action["decision"] == "deny"


def test_verification_is_bound_to_user():
    g = gate()
    verification, _ = stepup_for(g, request(user="alice", risk=90, assurance=1))
    stolen = g.decide(request(user="bob", risk=90, assurance=1,
                              verification=verification), now=1)
    assert stolen["decision"] == "deny"


def test_verification_is_single_use():
    g = gate()
    verification, _ = stepup_for(g, request(risk=90, assurance=1))
    first = g.decide(request(risk=90, assurance=1, verification=verification), now=0)
    assert first["decision"] == "allow"
    replay = g.decide(request(risk=90, assurance=1, verification=verification), now=0)
    assert replay["decision"] == "deny"
    assert replay["reason"] == "verification already used"


def test_method_downgrade_is_rejected_and_audited():
    g = gate()
    decision = g.decide(request(risk=90, assurance=1), now=0)
    weak = g.verify(decision["challenge_id"], "sms", now=0)
    assert weak["verified"] is False
    assert weak["reason"] == "method too weak"
    assert any(entry["outcome"] == "downgrade" for entry in g.audit())
    strong = g.verify(decision["challenge_id"], "hardware", now=0)
    assert strong["verified"] is True


def test_method_upgrade_is_accepted():
    g = gate()
    decision = g.decide(request(action="settings.update", risk=80, assurance=1), now=0)
    assert decision["decision"] == "stepup"
    verified = g.verify(decision["challenge_id"], "hardware", now=0)
    assert verified["verified"] is True
    allowed = g.decide(request(action="settings.update", risk=80, assurance=1,
                               verification=verified["verification"]), now=1)
    assert allowed["decision"] == "allow"


def test_trusted_device_requires_high_assurance_and_verification():
    g = gate()
    low = g.trust_device("alice", "phone-1", session_assurance=1, now=0)
    assert low["trusted"] is False
    assert low["reason"] == "insufficient assurance"
    decision = g.decide(request(action="device.trust", resource="phone-1",
                                risk=100, assurance=3), now=0)
    verification = g.verify(decision["challenge_id"], "hardware", now=0)["verification"]
    high = g.trust_device("alice", "phone-1", session_assurance=3,
                          verification=verification, now=1)
    assert high["trusted"] is True


def test_concurrent_requests_share_one_challenge_and_one_verification():
    g = gate()
    first = g.decide(request(risk=90, assurance=1), now=0)
    second = g.decide(request(risk=90, assurance=1), now=0)
    assert first["decision"] == second["decision"] == "stepup"
    assert first["challenge_id"] == second["challenge_id"]
    verification = g.verify(first["challenge_id"], "hardware", now=0)["verification"]
    assert g.decide(request(risk=90, assurance=1, verification=verification),
                    now=1)["decision"] == "allow"
    assert g.decide(request(risk=90, assurance=1, verification=verification),
                    now=1)["decision"] == "deny"


def test_decision_cache_is_keyed_by_resource():
    g = gate(cache_ttl=100)
    first = g.decide(request(resource="acct:42", risk=10, assurance=4), now=0)
    assert first["decision"] == "allow"
    cached = g.decide(request(resource="acct:42", risk=10, assurance=4), now=2)
    assert cached["decision"] == "allow"
    assert cached.get("cached") is True
    other = g.decide(request(resource="acct:99", risk=90, assurance=4), now=2)
    assert other["decision"] == "stepup"
