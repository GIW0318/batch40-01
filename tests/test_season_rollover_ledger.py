import pytest

import season_rollover_ledger as srl


RULES = {
    "master": "diamond",
    "diamond": "platinum",
    "platinum": "gold",
    "gold": "silver",
    "silver": "bronze",
    "bronze": "bronze",
}

REWARDS = {
    "master": 1000,
    "diamond": 500,
    "platinum": 200,
    "gold": 100,
    "silver": 50,
    "bronze": 10,
}


def make_players():
    return {
        "a": {"rank": "master", "peak": "master", "placement_done": True,
              "banned": False, "streak": 4, "tokens": 100},
        "b": {"rank": "diamond", "peak": "diamond", "placement_done": True,
              "banned": False, "streak": 2, "tokens": 200},
        "c": {"rank": "gold", "peak": "gold", "placement_done": True,
              "banned": False, "streak": 1, "tokens": 300},
        "d": {"rank": "silver", "peak": "silver", "placement_done": True,
              "banned": False, "streak": 5, "tokens": 400},
        "e": {"rank": "bronze", "peak": "bronze", "placement_done": True,
              "banned": False, "streak": 0, "tokens": 500},
    }


def make_ledger(players=None, batch_size=2, mail_ttl=1000.0, rules=None):
    ledger = srl.SeasonRolloverLedger(
        season_id="S1",
        reset_rules=rules or RULES,
        batch_size=batch_size,
        mail_ttl=mail_ttl,
        rewards=REWARDS,
    )
    ledger.load_players(players or make_players())
    return ledger


def test_roster_freezes_peak_and_ignores_later_player_mutation():
    ledger = make_ledger()
    roster = ledger.build_roster(now=0.0)
    entry = [item for item in roster["entries"] if item["account"] == "a"][0]
    assert entry["peak"] == "master"
    ledger.update_player("a", peak="bronze")
    assert [item for item in roster["entries"] if item["account"] == "a"][0]["peak"] == "master"


def test_grant_uses_peak_tier_and_records_current_tier_separately():
    ledger = make_ledger()
    ledger.update_player("a", rank="bronze", peak="master")
    ledger.build_roster(now=0.0)
    result = ledger.grant(now=100.0)
    mail = [item for item in result["mails"] if item["account"] == "a"][0]
    assert mail["peak_tier"] == "master"
    assert mail["current_tier"] == "bronze"
    assert mail["amount"] == 1000


def test_rerunning_grant_does_not_duplicate_mail():
    ledger = make_ledger()
    ledger.build_roster(now=0.0)
    first = ledger.grant(now=100.0)
    second = ledger.grant(now=200.0)
    assert len(first["mails"]) == 5
    assert second["duplicate"] is True
    assert len(second["mails"]) == 5


def test_mail_expiry_starts_at_actual_grant_time():
    ledger = make_ledger(mail_ttl=1000.0)
    ledger.build_roster(now=0.0)
    result = ledger.grant(now=500.0)
    mail = [item for item in result["mails"] if item["account"] == "a"][0]
    assert mail["expires_at"] == 1500.0


def test_banned_accounts_are_filtered_before_grant():
    players = make_players()
    players["a"]["banned"] = True
    ledger = make_ledger(players=players)
    ledger.build_roster(now=0.0)
    result = ledger.grant(now=100.0)
    accounts = [item["account"] for item in result["mails"]]
    assert "a" not in accounts
    assert len(accounts) == 4


def test_soft_reset_is_segmented_and_master_does_not_collapse_to_bronze():
    ledger = make_ledger()
    ledger.build_roster(now=0.0)
    ledger.reset()
    assert ledger.get_player("a")["rank"] == "diamond"
    assert ledger.get_player("b")["rank"] == "platinum"


def test_unplaced_account_is_not_reset_and_enters_pending_pool():
    players = make_players()
    players["new"] = {"rank": "gold", "peak": "gold", "placement_done": False,
                      "banned": False, "streak": 7, "tokens": 0}
    ledger = make_ledger(players=players)
    ledger.build_roster(now=0.0)
    result = ledger.reset()
    assert ledger.get_player("new")["rank"] == "gold"
    assert "new" in ledger.pending_accounts()
    assert "new" in result["pending_pool"]
    assert ledger.get_player("new")["streak"] == 0


def test_reset_resumes_from_checkpoint_after_partial_failure():
    ledger = make_ledger(batch_size=2)
    ledger.build_roster(now=0.0)
    first = ledger.reset(fail_after=1)
    assert first["complete"] is False
    assert first["processed"] == 2
    second = ledger.reset()
    assert second["complete"] is True
    assert second["processed"] == 5
    assert ledger.get_player("a")["rank"] == "diamond"
    assert ledger.get_player("b")["rank"] == "platinum"
    assert ledger.get_player("c")["rank"] == "silver"


def test_reset_clears_cross_season_win_streak():
    ledger = make_ledger()
    ledger.build_roster(now=0.0)
    ledger.reset()
    assert all(ledger.get_player(account)["streak"] == 0
               for account in ledger.player_accounts())


def test_reset_changes_current_rank_but_keeps_archived_peak():
    ledger = make_ledger()
    ledger.build_roster(now=0.0)
    ledger.reset()
    assert ledger.get_player("a")["rank"] == "diamond"
    assert ledger.get_player("a")["peak"] == "master"


def test_player_added_after_roster_build_is_not_granted():
    ledger = make_ledger()
    ledger.build_roster(now=0.0)
    ledger.add_player("late", rank="master", peak="master",
                      placement_done=True, banned=False, streak=0, tokens=0)
    result = ledger.grant(now=100.0)
    assert "late" not in [item["account"] for item in result["mails"]]


def test_old_tokens_are_reclaimed_once_after_grant():
    ledger = make_ledger()
    ledger.build_roster(now=0.0)
    ledger.grant(now=100.0)
    first = ledger.reclaim_tokens()
    second = ledger.reclaim_tokens()
    assert first["reclaimed"] == 1500
    assert second["reclaimed"] == 0
    assert all(ledger.get_player(account)["tokens"] == 0
               for account in ledger.player_accounts())


def test_claim_is_unique_per_account_and_tier():
    ledger = make_ledger()
    ledger.build_roster(now=0.0)
    ledger.grant(now=100.0)
    first = ledger.claim("a", "master", now=200.0)
    second = ledger.claim("a", "master", now=300.0)
    assert first["claimed"] is True
    assert first["amount"] == 1000
    assert second["claimed"] is False
    assert second["reason"] == "already_claimed"


def test_claim_after_mail_expiry_is_rejected():
    ledger = make_ledger(mail_ttl=100.0)
    ledger.build_roster(now=0.0)
    ledger.grant(now=0.0)
    result = ledger.claim("a", "master", now=101.0)
    assert result["claimed"] is False
    assert result["reason"] == "expired"


def test_custom_segmented_reset_rules_apply_to_each_tier():
    rules = {"master": "diamond", "diamond": "master", "gold": "platinum"}
    ledger = make_ledger(rules=rules)
    ledger.build_roster(now=0.0)
    ledger.reset()
    assert ledger.get_player("a")["rank"] == "diamond"
    assert ledger.get_player("b")["rank"] == "master"
    assert ledger.get_player("c")["rank"] == "platinum"
