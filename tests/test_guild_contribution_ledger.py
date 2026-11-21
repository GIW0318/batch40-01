import pytest

import guild_contribution_ledger as gcl


def make_guild(funds=1000, period_seconds=100.0, period_offset=20.0):
    ledger = gcl.GuildContributionLedger(period_seconds=period_seconds,
                                         period_offset=period_offset,
                                         wage_rate=1.0,
                                         compensation_cap=100)
    ledger.add_guild("g1", funds=funds)
    ledger.add_member("g1", "officer", role="officer")
    ledger.add_member("g1", "m1", role="member")
    return ledger


def test_duplicate_activity_report_accrues_only_once():
    ledger = make_guild()
    first = ledger.accrue("g1", "raid-1", "m1", 10, now=21.0, source="plugin-a")
    second = ledger.accrue("g1", "raid-1", "m1", 10, now=22.0, source="plugin-b")
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["applied"] == 0
    assert ledger.balance("g1", "m1") == 10


def test_period_boundary_follows_guild_reset_offset_not_natural_week():
    ledger = make_guild()
    ledger.accrue("g1", "before", "m1", 5, now=19.0)
    ledger.accrue("g1", "after", "m1", 7, now=20.0)
    previous = ledger.ledger("g1", period=-1)
    current = ledger.ledger("g1", period=0)
    assert [entry["activity_id"] for entry in previous] == ["before"]
    assert [entry["activity_id"] for entry in current] == ["after"]


def test_redeem_deducts_balance_before_granting_item():
    ledger = make_guild()
    ledger.accrue("g1", "raid-1", "m1", 100, now=21.0)
    result = ledger.redeem("g1", "m1", 30, "flask", request_id="redeem-1", now=22.0)
    assert result["redeemed"] is True
    assert result["cost"] == 30
    assert result["balance"] == 70
    assert ledger.balance("g1", "m1") == 70


def test_duplicate_redemption_request_grants_once():
    ledger = make_guild()
    ledger.accrue("g1", "raid-1", "m1", 100, now=21.0)
    first = ledger.redeem("g1", "m1", 30, "flask", request_id="redeem-1", now=22.0)
    second = ledger.redeem("g1", "m1", 30, "flask", request_id="redeem-1", now=23.0)
    assert first["redeemed"] is True
    assert second["duplicate"] is True
    assert ledger.balance("g1", "m1") == 70
    assert len([entry for entry in ledger.ledger("g1")
                if entry["kind"] == "redeem"]) == 1


def test_second_competing_redemption_fails_without_negative_balance():
    ledger = make_guild()
    ledger.accrue("g1", "raid-1", "m1", 50, now=21.0)
    assert ledger.redeem("g1", "m1", 40, "flask", request_id="r1", now=22.0)["redeemed"] is True
    with pytest.raises(ValueError):
        ledger.redeem("g1", "m1", 40, "flask", request_id="r2", now=23.0)
    assert ledger.balance("g1", "m1") == 10


def test_member_who_left_keeps_history_but_gets_no_wage():
    ledger = make_guild()
    ledger.accrue("g1", "raid-1", "m1", 100, now=21.0)
    ledger.leave("g1", "m1")
    result = ledger.settle("g1", period=0, now=25.0)
    assert result["total"] == 0
    assert result["wages"] == []
    assert ledger.balance("g1", "m1") == 100
    assert [entry["kind"] for entry in ledger.ledger("g1", period=0)] == ["accrue"]


def test_rejoined_member_is_not_paid_for_closed_period():
    ledger = make_guild()
    ledger.accrue("g1", "raid-1", "m1", 100, now=21.0)
    ledger.leave("g1", "m1")
    ledger.join("g1", "m1", now=22.0)
    closed = ledger.settle("g1", period=0, now=23.0)
    assert closed["total"] == 0
    ledger.accrue("g1", "raid-2", "m1", 50, now=121.0)
    reopened = ledger.settle("g1", period=1, now=122.0)
    assert reopened["total"] == 50
    assert [wage["member"] for wage in reopened["wages"]] == ["m1"]


def test_officer_compensation_is_capped():
    ledger = make_guild()
    with pytest.raises(ValueError):
        ledger.compensate("g1", "officer", "m1", 150, "makeup", now=21.0)
    assert ledger.balance("g1", "m1") == 0


def test_compensation_requires_officer_role():
    ledger = make_guild()
    with pytest.raises(ValueError):
        ledger.compensate("g1", "m1", "m1", 10, "self-service", now=21.0)
    assert ledger.balance("g1", "m1") == 0


def test_settle_fails_on_insufficient_funds_then_is_idempotent():
    ledger = make_guild(funds=0)
    ledger.accrue("g1", "raid-1", "m1", 100, now=21.0)
    with pytest.raises(ValueError):
        ledger.settle("g1", period=0, now=22.0)
    assert [entry["kind"] for entry in ledger.ledger("g1")] == ["accrue"]
    ledger.add_funds("g1", 100)
    first = ledger.settle("g1", period=0, now=23.0)
    second = ledger.settle("g1", period=0, now=24.0)
    assert first["total"] == 100
    assert second["duplicate"] is True
    assert second["total"] == 100
    assert ledger.guild_funds("g1") == 0
    assert len([entry for entry in ledger.ledger("g1")
                if entry["kind"] == "wage"]) == 1


def test_cross_week_compensation_does_not_rewrite_closed_period():
    ledger = make_guild()
    ledger.accrue("g1", "raid-1", "m1", 10, now=21.0)
    ledger.settle("g1", period=0, now=22.0)
    closed_before = ledger.ledger("g1", period=0)
    ledger.compensate("g1", "officer", "m1", 5, "late-report", now=121.0)
    assert ledger.ledger("g1", period=0) == closed_before
    current = ledger.ledger("g1", period=1)
    assert len(current) == 1
    assert current[0]["kind"] == "compensate"
    assert current[0]["amount"] == 5


def test_leaderboard_ties_break_by_first_contribution_not_join_time():
    ledger = make_guild()
    ledger.add_member("g1", "early_joiner", joined_at=0.0)
    ledger.add_member("g1", "late_joiner", joined_at=50.0)
    ledger.accrue("g1", "activity-a", "early_joiner", 10, now=90.0)
    ledger.accrue("g1", "activity-b", "late_joiner", 10, now=80.0)
    ranking = ledger.leaderboard("g1", period=0)
    assert [entry["member"] for entry in ranking][:2] == ["late_joiner", "early_joiner"]
    assert ranking[0]["contribution"] == ranking[1]["contribution"] == 10


def test_public_ledger_hides_internal_actor_and_note():
    ledger = make_guild()
    ledger.compensate("g1", "officer", "m1", 10, "internal-note", now=21.0)
    private = ledger.ledger("g1")
    public = ledger.ledger("g1", public=True)
    assert len(private) == len(public) == 1
    assert private[0]["actor"] == "officer"
    assert private[0]["internal_note"] == "internal-note"
    assert "actor" not in public[0]
    assert "internal_note" not in public[0]


def test_settlement_ledger_records_member_and_wage_source():
    ledger = make_guild()
    ledger.accrue("g1", "raid-1", "m1", 50, now=21.0)
    result = ledger.settle("g1", period=0, now=22.0)
    assert result["wages"][0]["member"] == "m1"
    assert result["wages"][0]["amount"] == 50
    wage = [entry for entry in ledger.ledger("g1", period=0)
            if entry["kind"] == "wage"][0]
    assert wage["member"] == "m1"
    assert wage["source"] == "wage"
    assert wage["amount"] == 50


def test_ledger_is_partitioned_by_guild_and_period():
    ledger = make_guild()
    ledger.add_guild("g2", funds=1000)
    ledger.add_member("g2", "m2")
    ledger.accrue("g1", "raid-1", "m1", 10, now=21.0)
    ledger.accrue("g2", "raid-2", "m2", 20, now=21.0)
    ledger.accrue("g1", "raid-3", "m1", 30, now=121.0)
    first = ledger.ledger("g1")
    second = ledger.ledger("g2")
    assert {entry["guild_id"] for entry in first} == {"g1"}
    assert {entry["guild_id"] for entry in second} == {"g2"}
    assert [entry["activity_id"] for entry in ledger.ledger("g1", period=1)] == ["raid-3"]
