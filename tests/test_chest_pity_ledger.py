import pytest

import chest_pity_ledger as clf


def make(hard_pity=100, inherit=False):
    ledger = clf.ChestPityLedger()
    ledger.configure_pool(
        "standard",
        "gold",
        hard_pity,
        pool_version="pv1",
        prob_version="base",
        inherit_across_seasons=inherit,
    )
    ledger.login("acct")
    return ledger


def test_open_requires_login():
    ledger = clf.ChestPityLedger()
    ledger.configure_pool("standard", "gold", 10)
    with pytest.raises(RuntimeError):
        ledger.open("acct", "standard", "gold")


def test_relogin_keeps_pity():
    ledger = make()
    ledger.open("acct", "standard", "gold", count=4, request_id="r1")
    assert ledger.pity("acct", "standard", "gold") == 4
    ledger.login("acct")
    assert ledger.pity("acct", "standard", "gold") == 4


def test_ten_pull_returns_individually_indexed_items():
    ledger = make()
    result = ledger.open("acct", "standard", "gold", count=10, request_id="r10")
    assert len(result["items"]) == 10
    assert [item["index"] for item in result["items"]] == list(range(1, 11))
    assert len({item["item_id"] for item in result["items"]}) == 10


def test_ten_pull_settles_pity_once_for_the_batch():
    ledger = make(hard_pity=15)
    ledger.open("acct", "standard", "gold", count=10, request_id="a")
    assert ledger.pity("acct", "standard", "gold") == 10
    result = ledger.open("acct", "standard", "gold", count=10, request_id="b")
    assert result["pity_before"] == 10
    assert result["pity_after"] == 0
    assert [item["rarity"] for item in result["items"]].count("rare") == 1
    assert result["items"][-1]["rarity"] == "rare"


def test_pity_is_separate_per_quality():
    ledger = make()
    ledger.configure_pool("standard", "silver", 100)
    ledger.open("acct", "standard", "gold", count=3, request_id="g1")
    assert ledger.pity("acct", "standard", "gold") == 3
    assert ledger.pity("acct", "standard", "silver") == 0


def test_pity_is_separate_per_account():
    ledger = make()
    ledger.login("second")
    ledger.open("acct", "standard", "gold", count=2, request_id="a1")
    ledger.open("second", "standard", "gold", count=5, request_id="b1")
    assert ledger.pity("acct", "standard", "gold") == 2
    assert ledger.pity("second", "standard", "gold") == 5


def test_request_id_is_idempotent():
    ledger = make()
    first = ledger.open("acct", "standard", "gold", count=10, request_id="dup")
    second = ledger.open("acct", "standard", "gold", count=10, request_id="dup")
    assert second == first
    assert ledger.pity("acct", "standard", "gold") == 10
    assert len(ledger.history("acct")) == 1


def test_rate_up_window_uses_boosted_table():
    ledger = make()
    ledger.configure_pool("promo", "gold", 100, prob_version="base")
    ledger.set_rate_up("promo", start=100, end=200, prob_version="boost")
    result = ledger.open(
        "acct", "promo", "gold", count=1, request_id="p1", now=150
    )
    assert result["prob_version"] == "boost"


def test_rate_up_window_expires():
    ledger = make()
    ledger.configure_pool("promo", "gold", 100, prob_version="base")
    ledger.set_rate_up("promo", start=100, end=200, prob_version="boost")
    result = ledger.open(
        "acct", "promo", "gold", count=1, request_id="p2", now=200
    )
    assert result["prob_version"] == "base"


def test_history_records_full_open_details():
    ledger = make()
    ledger.open("acct", "standard", "gold", count=1, request_id="h1", now=5)
    record = ledger.history("acct")[0]
    required = {
        "request_id",
        "account",
        "pool",
        "quality",
        "count",
        "items",
        "pity_before",
        "pity_after",
        "pool_version",
        "prob_version",
        "season",
        "at",
        "rolled_back",
    }
    assert required <= set(record)
    assert record["request_id"] == "h1"
    assert record["pool_version"] == "pv1"
    assert record["prob_version"] == "base"
    assert record["season"] == 1
    assert record["at"] == 5
    assert record["rolled_back"] is False


def test_rollback_restores_pity_counter():
    ledger = make()
    ledger.open("acct", "standard", "gold", count=3, request_id="r3")
    ledger.rollback("r3")
    assert ledger.pity("acct", "standard", "gold") == 0
    assert ledger.history("acct")[0]["rolled_back"] is True


def test_rollback_twice_is_rejected():
    ledger = make()
    ledger.open("acct", "standard", "gold", count=1, request_id="r4")
    ledger.rollback("r4")
    with pytest.raises(ValueError):
        ledger.rollback("r4")
    with pytest.raises(ValueError):
        ledger.rollback("missing")


def test_cross_season_inherits_when_configured():
    ledger = make(hard_pity=100, inherit=True)
    ledger.open("acct", "standard", "gold", count=4, request_id="s1")
    assert ledger.rollover("acct", "standard", "gold", 2) == 4
    assert ledger.pity("acct", "standard", "gold") == 4


def test_cross_season_resets_by_default():
    ledger = make(hard_pity=100, inherit=False)
    ledger.open("acct", "standard", "gold", count=4, request_id="s2")
    assert ledger.rollover("acct", "standard", "gold", 2) == 0
    assert ledger.pity("acct", "standard", "gold") == 0


def test_invalid_count_and_unknown_pool_are_rejected():
    ledger = make()
    with pytest.raises(ValueError):
        ledger.open("acct", "standard", "gold", count=11)
    with pytest.raises(ValueError):
        ledger.open("acct", "missing", "gold")
