import pytest

import season_leaderboard_freeze as slf


TIERS = [
    {"min_rank": 1, "max_rank": 1, "tier": "champion", "reward": 100},
    {"min_rank": 2, "max_rank": 2, "tier": "silver", "reward": 50},
    {"min_rank": 3, "max_rank": 10, "tier": "bronze", "reward": 10},
]


def make_board(top_n=100, tiers=None):
    return slf.SeasonLeaderboardFreeze(season_id="S1", season_end=100.0,
                                       top_n=top_n, tiers=tiers or TIERS)


def test_submit_before_freeze_updates_live_rank():
    board = make_board()
    board.submit("a", 100, match_id="m1", ended_at=10.0, submitted_at=10.0)
    board.submit("b", 200, match_id="m2", ended_at=11.0, submitted_at=11.0)
    items = board.rank()["items"]
    assert [item["account"] for item in items] == ["b", "a"]
    assert [item["rank"] for item in items] == [1, 2]


def test_freeze_rejects_later_submissions():
    board = make_board()
    board.submit("a", 100, ended_at=10.0, submitted_at=10.0)
    board.submit("b", 90, ended_at=11.0, submitted_at=11.0)
    board.freeze(now=100.0)
    result = board.submit("c", 300, match_id="late", ended_at=50.0,
                          submitted_at=101.0)
    assert result["accepted"] is False
    assert result["reason"] == "frozen"
    assert board.rank()["total"] == 2


def test_late_match_before_season_end_uses_appeal_channel():
    board = make_board()
    board.submit("a", 100, ended_at=10.0, submitted_at=10.0)
    board.freeze(now=100.0)
    rejected = board.submit("c", 300, match_id="late", ended_at=50.0,
                            submitted_at=101.0)
    assert rejected["accepted"] is False
    appealed = board.appeal("c", 300, case_id="case-1", now=102.0)
    assert appealed["appealed"] is True
    items = board.rank()["items"]
    assert items[0]["account"] == "c"
    assert items[0]["score"] == 300


def test_match_ending_after_season_end_belongs_to_next_season():
    board = make_board()
    result = board.submit("c", 300, match_id="m9", ended_at=101.0,
                          submitted_at=50.0)
    assert result["accepted"] is False
    assert result["reason"] == "next_season"
    assert board.rank()["total"] == 0


def test_match_ending_at_season_end_counts_for_this_season():
    board = make_board()
    result = board.submit("c", 250, match_id="m9", ended_at=100.0,
                          submitted_at=90.0)
    assert result["accepted"] is True
    assert board.rank()["items"][0]["account"] == "c"


def test_tied_scores_share_rank_and_order_is_stable():
    board = make_board()
    board.submit("a", 100, ended_at=10.0, submitted_at=10.0)
    board.submit("b", 100, ended_at=11.0, submitted_at=11.0)
    board.submit("c", 50, ended_at=12.0, submitted_at=12.0)
    first = board.rank()["items"]
    second = board.rank()["items"]
    assert [(item["account"], item["rank"]) for item in first] == [
        ("a", 1), ("b", 1), ("c", 3)
    ]
    assert [item["account"] for item in first] == [item["account"] for item in second]


def test_award_uses_shared_tier_for_tied_ranks():
    board = make_board()
    board.submit("a", 100, ended_at=10.0, submitted_at=10.0)
    board.submit("b", 100, ended_at=11.0, submitted_at=11.0)
    board.submit("c", 50, ended_at=12.0, submitted_at=12.0)
    board.freeze(now=100.0)
    awards = board.award()["awards"]
    by_account = {item["account"]: item for item in awards}
    assert by_account["a"]["tier"] == "champion"
    assert by_account["b"]["tier"] == "champion"
    assert by_account["a"]["amount"] == by_account["b"]["amount"] == 100
    assert by_account["c"]["tier"] == "bronze"


def test_cheater_removal_backfills_ranks():
    board = make_board()
    for index, account in enumerate(["a", "b", "c", "d"]):
        board.submit(account, 100 - index * 10, ended_at=float(index),
                     submitted_at=float(index))
    board.freeze(now=100.0)
    board.remove("b", reason="cheat", now=101.0)
    items = board.rank()["items"]
    assert [item["account"] for item in items] == ["a", "c", "d"]
    assert [item["rank"] for item in items] == [1, 2, 3]
    assert "b" not in [item["account"] for item in board.award()["awards"]]


def test_merge_server_deduplicates_account_and_keeps_best_score():
    board = make_board()
    board.merge_server("s1", [{"account": "a", "score": 100},
                              {"account": "b", "score": 50}])
    board.merge_server("s2", [{"account": "a", "score": 150},
                              {"account": "c", "score": 70}])
    items = board.rank()["items"]
    assert [item["account"] for item in items] == ["a", "c", "b"]
    assert items[0]["score"] == 150
    assert len(items) == 3


def test_pagination_is_exact_and_stable_after_freeze():
    board = make_board(top_n=100)
    for index in range(103):
        board.submit("p%03d" % index, 1000 - index, ended_at=10.0,
                     submitted_at=10.0)
    board.freeze(now=100.0)
    seen = []
    for page in range(1, 11):
        result = board.rank(page=page, page_size=10)
        assert len(result["items"]) == 10
        seen.extend(item["account"] for item in result["items"])
    assert len(seen) == len(set(seen)) == 100
    second_page = [item["account"] for item in board.rank(page=2, page_size=10)["items"]]
    late = board.submit("late", 9999, ended_at=50.0, submitted_at=101.0)
    assert late["accepted"] is False
    again = [item["account"] for item in board.rank(page=2, page_size=10)["items"]]
    assert again == second_page


def test_freeze_is_idempotent():
    board = make_board()
    board.submit("a", 100, ended_at=10.0, submitted_at=10.0)
    first = board.freeze(now=100.0)
    second = board.freeze(now=200.0)
    assert second["version"] == first["version"]
    assert second["frozen_at"] == 100.0
    assert second["entries"] == first["entries"]


def test_post_freeze_merge_is_rejected_and_award_reads_frozen_list():
    board = make_board()
    board.submit("a", 100, ended_at=10.0, submitted_at=10.0)
    board.submit("b", 90, ended_at=11.0, submitted_at=11.0)
    board.freeze(now=100.0)
    with pytest.raises(ValueError):
        board.merge_server("s2", [{"account": "c", "score": 999}])
    assert board.submit("c", 999, ended_at=50.0, submitted_at=101.0)["accepted"] is False
    accounts = [item["account"] for item in board.award()["awards"]]
    assert accounts == ["a", "b"]


def test_appeal_after_award_supplements_the_missing_reward():
    board = make_board()
    board.submit("a", 100, ended_at=10.0, submitted_at=10.0)
    board.submit("b", 90, ended_at=11.0, submitted_at=11.0)
    board.freeze(now=100.0)
    first = board.award()
    assert {item["account"]: item["amount"] for item in first["awards"]} == {
        "a": 100, "b": 50
    }
    board.appeal("b", 200, case_id="case-2", now=102.0)
    second = board.award()
    supplement = {item["account"]: item["amount"] for item in second["supplement"]}
    assert supplement == {"b": 50}
    assert board.awarded_amount("b") == 100


def test_award_can_be_queried_by_frozen_version():
    board = make_board()
    board.submit("a", 100, ended_at=10.0, submitted_at=10.0)
    board.submit("b", 90, ended_at=11.0, submitted_at=11.0)
    board.freeze(now=100.0)
    original = board.award(version=1)
    board.appeal("b", 200, case_id="case-3", now=102.0)
    current = board.award()
    old = board.award(version=1)
    assert old["awards"] == original["awards"]
    assert current["version"] > 1
    assert old["version"] == 1


def test_award_only_covers_top_n_and_defined_tiers():
    board = make_board(top_n=2)
    board.submit("a", 100, ended_at=10.0, submitted_at=10.0)
    board.submit("b", 90, ended_at=11.0, submitted_at=11.0)
    board.submit("c", 80, ended_at=12.0, submitted_at=12.0)
    board.freeze(now=100.0)
    result = board.award()
    assert [item["account"] for item in result["awards"]] == ["a", "b"]
    assert board.rank()["total"] == 2
