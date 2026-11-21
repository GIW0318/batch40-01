import pytest

import token_revocation_cache as trc


def cache(**kwargs):
    return trc.TokenRevocationCache(**kwargs)


def check(cache_obj, token="t1", issuer="issuer-a", subject="alice",
          issued=100, expires=1000, now=500):
    return cache_obj.check(issuer, token, subject, issued, expires, now)


def test_valid_token_is_allowed():
    result = check(cache())
    assert result["allowed"] is True
    assert result["reason"] is None


def test_expiry_boundary_is_exclusive():
    c = cache()
    before = check(c, expires=500, now=499)
    at_boundary = check(c, expires=500, now=500)
    assert before["allowed"] is True
    assert at_boundary["allowed"] is False
    assert at_boundary["reason"] == "expired"


def test_caller_supplied_now_controls_both_gateways():
    first = cache()
    second = cache()
    early = check(first, expires=150, now=100)
    late = check(second, expires=150, now=200)
    assert early["allowed"] is True
    assert late["allowed"] is False
    assert late["reason"] == "expired"


def test_revoke_token_only_affects_one_device():
    c = cache()
    assert check(c, token="phone")["allowed"] is True
    assert check(c, token="laptop")["allowed"] is True
    c.revoke_token("issuer-a", "phone", now=200)
    assert check(c, token="phone", now=300)["allowed"] is False
    assert check(c, token="laptop", now=300)["allowed"] is True


def test_account_revocation_is_bulk_and_subject_scoped():
    c = cache()
    check(c, token="t1", subject="alice")
    check(c, token="t2", subject="alice")
    check(c, token="t3", subject="bob")
    revoked = c.revoke_account("issuer-a", "alice", 500)
    assert revoked == 2
    assert check(c, token="t1", subject="alice", now=600)["allowed"] is False
    assert check(c, token="t2", subject="alice", now=600)["allowed"] is False
    assert check(c, token="t3", subject="bob", now=600)["allowed"] is True


def test_token_issued_after_account_cutoff_survives():
    c = cache()
    check(c, token="old", subject="alice", issued=400)
    c.revoke_account("issuer-a", "alice", 500)
    fresh = check(c, token="new", subject="alice", issued=600, now=700)
    assert fresh["allowed"] is True


def test_same_token_id_under_another_issuer_is_not_revoked():
    c = cache()
    check(c, token="t1", issuer="issuer-a")
    c.revoke_token("issuer-a", "t1", now=200)
    assert check(c, token="t1", issuer="issuer-a", now=300)["allowed"] is False
    assert check(c, token="t1", issuer="issuer-b", now=300)["allowed"] is True


def test_negative_cache_is_invalidated_by_revocation():
    c = cache()
    assert check(c, token="target", now=100)["allowed"] is True
    c.revoke_token("issuer-a", "target", now=110)
    after = check(c, token="target", now=120)
    assert after["allowed"] is False
    assert after["reason"] == "revoked_token"


def test_older_revocation_snapshot_cannot_undo_newer_state():
    c = cache()
    newer = {"issuer": "issuer-a", "token_id": "t1", "revoked": True, "seq": 10}
    older = {"issuer": "issuer-a", "token_id": "t1", "revoked": False, "seq": 5}
    first = c.apply_revocations([newer], now=100)
    second = c.apply_revocations([older], now=110)
    assert first["applied"] == 1
    assert second["ignored"] >= 1
    assert check(c, token="t1", now=200)["allowed"] is False


def test_revocation_events_are_idempotent():
    c = cache()
    event = {"issuer": "issuer-a", "token_id": "t1", "revoked": True, "seq": 10}
    c.apply_revocations([event], now=100)
    again = c.apply_revocations([event], now=110)
    assert again["applied"] == 0
    assert again["ignored"] == 1
    assert again["last_seq"] == 10


def test_negative_cache_is_bounded():
    c = cache(negative_limit=3)
    for index in range(5):
        result = check(c, token="scan-%d" % index, expires=10000, now=100)
        assert result["allowed"] is True
    info = c.snapshot()
    assert info["negative_entries"] <= 3
    assert info["evictions"] >= 2


def test_cache_hits_and_misses_are_counted():
    c = cache()
    check(c, token="t1", now=100)
    check(c, token="t1", now=101)
    info = c.snapshot()
    assert info["misses"] == 1
    assert info["hits"] >= 1


def test_snapshot_returns_isolated_copy():
    c = cache()
    c.revoke_token("issuer-a", "t1", now=100)
    first = c.snapshot()["revoked_tokens"]
    assert len(first) == 1
    first.append({"issuer": "evil", "token_id": "ghost"})
    second = c.snapshot()["revoked_tokens"]
    assert len(second) == 1


def test_revocation_reason_is_preserved():
    c = cache()
    c.revoke_token("issuer-a", "t1", now=100, reason="security")
    result = check(c, token="t1", now=200)
    assert result["reason"] == "revoked_token"
    entry = c.snapshot()["revoked_tokens"][0]
    assert entry["reason"] == "security"


def test_revoked_token_stays_revoked_after_expiry():
    c = cache()
    c.revoke_token("issuer-a", "t1", now=100)
    result = check(c, token="t1", expires=500, now=2000)
    assert result["allowed"] is False
    assert result["reason"] == "revoked_token"
