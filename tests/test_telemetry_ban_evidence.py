from datetime import datetime, timedelta

import pytest

import telemetry_ban_evidence as tbe


def flag(seq, t):
    return {"seq": seq, "t": t, "kind": "flag"}


def sample(seq, t):
    return {"seq": seq, "t": t, "kind": "sample"}


def gap(seq, t):
    return {"seq": seq, "t": t, "kind": "gap"}


def build(threshold=3, **kwargs):
    return tbe.TelemetryBanEvidence(threshold=threshold, **kwargs)


def test_duplicate_report_is_ignored():
    service = build(threshold=5)
    assert service.ingest("acct", 1, [flag(1, 100)], report_id="dup") is True
    assert service.ingest("acct", 1, [flag(2, 110)], report_id="dup") is False
    assert service.evaluate("acct")["flags"] == 1


def test_events_are_reordered_by_server_time():
    service = build(threshold=5)
    service.ingest("acct", 1, [flag(2, 200), sample(1, 100)])
    events = service.evidence("acct")[0]["events"]
    assert [event["t"] for event in events] == [100, 200]
    assert [event["seq"] for event in events] == [1, 2]


def test_missing_sample_is_undecidable_not_a_flag():
    service = build(threshold=1)
    service.ingest("acct", 1, [gap(1, 100)])
    result = service.evaluate("acct")
    assert result["flags"] == 0
    assert result["undecidable"] == 1
    assert result["eligible"] is False


def test_threshold_accumulates_within_one_natural_day():
    service = build(threshold=2)
    service.ingest("acct", 1, [flag(1, 100), flag(2, 200)])
    assert service.evaluate("acct")["eligible"] is True


def test_threshold_does_not_accumulate_across_days():
    service = build(threshold=2)
    service.ingest("acct", 1, [flag(1, 100), flag(2, 90000)])
    result = service.evaluate("acct")
    assert max(result["per_day"].values()) == 1
    assert result["eligible"] is False


def test_evaluate_is_stable_across_reconnects():
    service = build(threshold=99)
    service.ingest("acct", 1, [flag(1, 100)])
    first = service.evaluate("acct")
    second = service.evaluate("acct")
    assert first["flags"] == second["flags"] == 1
    service.ingest("acct", 2, [flag(1, 200)])
    assert service.evaluate("acct")["flags"] == 2


def test_hash_chain_is_complete_when_segments_link():
    service = build(threshold=5)
    service.ingest("acct", 1, [flag(1, 100)], segment=1,
                   segment_hash="h1")
    service.ingest("acct", 2, [flag(1, 200)], segment=2,
                   prev_hash="h1", segment_hash="h2")
    assert service.chain_status("acct") == {
        "complete": True,
        "broken_at": None,
    }


def test_hash_chain_break_is_localized_to_segment():
    service = build(threshold=5)
    service.ingest("acct", 1, [flag(1, 100)], segment=1,
                   segment_hash="h1")
    service.ingest("acct", 2, [flag(1, 200)], segment=2,
                   prev_hash="h1", segment_hash="h2")
    service.ingest("acct", 3, [flag(1, 300)], segment=3,
                   prev_hash="bad", segment_hash="h3")
    assert service.chain_status("acct") == {
        "complete": False,
        "broken_at": 3,
    }


def test_ban_is_rejected_when_chain_is_broken():
    service = build(threshold=1)
    service.ingest("acct", 1, [flag(1, 100)], segment=1,
                   segment_hash="h1")
    service.ingest("acct", 2, [flag(1, 200)], segment=2,
                   prev_hash="bad", segment_hash="h2")
    result = service.evaluate("acct")
    assert result["flags"] == 2
    assert result["chain_complete"] is False
    with pytest.raises(ValueError):
        service.ban("acct", reason="aim")
    assert service.state("acct")["status"] == "active"


def test_whitelisted_account_cannot_be_banned():
    service = build(threshold=1)
    service.ingest("acct", 1, [flag(1, 100)])
    service.set_whitelist("acct", True)
    result = service.evaluate("acct")
    assert result["whitelisted"] is True
    assert result["eligible"] is False
    with pytest.raises(ValueError):
        service.ban("acct", reason="aim")


def test_ban_sets_state_with_utc_timestamp():
    service = build(threshold=1)
    service.ingest("acct", 1, [flag(1, 100)])
    service.ban("acct", reason="aim", now=1700000000)
    state = service.state("acct")
    assert state["status"] == "banned"
    assert state["changed_at"].endswith("+00:00")
    parsed = datetime.fromisoformat(state["changed_at"])
    assert parsed.utcoffset() == timedelta(0)


def test_appeal_unbans_and_marks_ban_invalid():
    service = build(threshold=1)
    service.ingest("acct", 1, [flag(1, 100)])
    service.ban("acct", reason="aim", now=1000)
    service.appeal("acct", now=2000)
    assert service.state("acct")["status"] == "active"
    records = service.history("acct")
    assert len(records) == 1
    assert records[0]["valid"] is False


def test_appeal_requires_banned_state():
    service = build(threshold=1)
    with pytest.raises(ValueError):
        service.appeal("acct", now=1000)


def test_reban_requires_fresh_evidence_after_appeal():
    service = build(threshold=1)
    service.ingest("acct", 1, [flag(1, 100)], received_at=10)
    assert service.evaluate("acct")["eligible"] is True
    service.ban("acct", reason="aim", now=1000)
    service.appeal("acct", now=2000)
    after = service.evaluate("acct")
    assert after["fresh_flags"] == 0
    assert after["eligible"] is False
    with pytest.raises(ValueError):
        service.ban("acct", reason="aim", now=3000)
    service.ingest("acct", 2, [flag(1, 200)], received_at=3000)
    assert service.evaluate("acct")["eligible"] is True
    service.ban("acct", reason="aim", now=4000)
    records = service.history("acct")
    assert len(records) == 2
    assert records[0]["valid"] is False
    assert records[1]["valid"] is True


def test_retention_must_cover_appeal_window():
    with pytest.raises(ValueError):
        tbe.TelemetryBanEvidence(retention_days=10, appeal_days=30)
    service = tbe.TelemetryBanEvidence(
        threshold=5, retention_days=90, appeal_days=30
    )
    service.ingest("acct", 1, [flag(1, 100)], received_at=0)
    assert service.purge(now=89 * 86400) == 0
    assert len(service.evidence("acct")) == 1
    assert service.purge(now=91 * 86400) == 1
    assert service.evidence("acct") == []
