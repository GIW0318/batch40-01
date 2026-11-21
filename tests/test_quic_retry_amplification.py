import pytest

import quic_retry_amplification as qra


ADDR = "10.0.0.1:4433"


def guard(**kwargs):
    return qra.QuicRetryGuard(**kwargs)


def test_check_initial_without_token_requests_retry():
    result = guard().check_initial("cid-1", ADDR, 1200, 0)
    assert result["action"] == "retry"
    assert result["token"]


def test_valid_retry_token_is_accepted():
    gate = guard()
    result = gate.check_initial("cid-1", ADDR, 1200, 0)
    accepted = gate.check_initial("cid-1", ADDR, 1200, 1, token=result["token"])
    assert accepted["action"] == "accept"
    assert gate.snapshot()["accepted"] == 1


def test_token_is_bound_to_source_port():
    gate = guard()
    token = gate.issue_retry("cid-1", ADDR, 0)["token"]
    assert gate.validate_token(token, "10.0.0.1:5000", "cid-1", 1) is False
    assert gate.validate_token(token, ADDR, "cid-1", 1) is True


def test_token_is_bound_to_initial_connection():
    gate = guard()
    token = gate.issue_retry("cid-1", ADDR, 0)["token"]
    assert gate.validate_token(token, ADDR, "cid-2", 1) is False


def test_key_rotation_invalidates_old_tokens():
    gate = guard()
    token = gate.issue_retry("cid-1", ADDR, 0)["token"]
    assert gate.validate_token(token, ADDR, "cid-1", 1) is True
    gate.rotate_key()
    assert gate.validate_token(token, ADDR, "cid-1", 2) is False


def test_token_expires_after_ttl():
    gate = guard(token_ttl=10.0)
    token = gate.issue_retry("cid-1", ADDR, 0)["token"]
    assert gate.validate_token(token, ADDR, "cid-1", 9.5) is True
    assert gate.validate_token(token, ADDR, "cid-1", 10.5) is False


def test_backwards_monotonic_clock_is_rejected():
    gate = guard()
    token = gate.issue_retry("cid-1", ADDR, 100)["token"]
    with pytest.raises(ValueError):
        gate.validate_token(token, ADDR, "cid-1", 50)


def test_malformed_token_does_not_change_state():
    gate = guard()
    before = gate.snapshot()
    assert gate.validate_token("not-a-token", ADDR, "cid-1", 1) is False
    assert gate.validate_token("", ADDR, "cid-1", 1) is False
    assert gate.snapshot() == before


def test_retry_token_does_not_begin_with_static_prefix():
    gate = guard(retry_prefix="c0ffee")
    token = gate.issue_retry("cid-1", ADDR, 0)["token"]
    assert not token.startswith("c0ffee")


def test_amplification_budget_is_three_times_received():
    gate = guard()
    gate.check_initial("cid-1", ADDR, 1000, 0)
    assert gate.send_budget(ADDR) == 3000
    assert gate.charge_send(ADDR, 3000) == 0
    assert gate.send_budget(ADDR) == 0


def test_amplification_budget_is_shared_across_connections():
    gate = guard()
    gate.check_initial("cid-1", ADDR, 1000, 0)
    gate.check_initial("cid-2", ADDR, 1000, 0)
    assert gate.send_budget(ADDR) == 6000
    assert gate.charge_send(ADDR, 6000) == 0
    with pytest.raises(ValueError):
        gate.charge_send(ADDR, 1)
    assert gate.snapshot()["rejected"]["amplification"] == 1


def test_migration_to_new_ip_requires_path_challenge():
    gate = guard()
    result = gate.on_migration("cid-1", ADDR, "10.0.0.9:4433", 0)
    assert result["migrated"] is True
    assert result["challenge"]
    assert gate.validate_path_challenge("cid-1", result["challenge"], ADDR, 1) is False
    assert gate.validate_path_challenge("cid-1", result["challenge"], "10.0.0.9:4433", 1) is True
    assert gate.snapshot()["migrations"] == 1


def test_nat_rebind_is_not_treated_as_migration():
    gate = guard()
    result = gate.on_migration("cid-1", ADDR, "10.0.0.1:5555", 0)
    assert result["migrated"] is False
    assert result["challenge"] is None
    assert gate.snapshot()["nat_rebinds"] == 1
    assert gate.snapshot()["migrations"] == 0


def test_0rtt_ticket_replay_is_rejected():
    gate = guard()
    first = gate.on_0rtt("cid-1", "ticket-1", 0)
    second = gate.on_0rtt("cid-2", "ticket-1", 1)
    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["reason"] == "replay"
    assert gate.snapshot()["0rtt_replays"] == 1


def test_snapshot_splits_rejection_reasons():
    gate = guard()
    result = gate.check_initial("cid-1", ADDR, 1200, 0, token="broken")
    assert result["action"] == "retry"
    snapshot = gate.snapshot()
    assert snapshot["rejected"]["bad_token"] == 1
    assert snapshot["rejected"]["amplification"] == 0
    assert snapshot["retry_issued"] == 1
