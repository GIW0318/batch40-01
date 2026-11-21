import pytest

import quest_share_relay as qsr


QUESTS = {
    "q1": {},
    "q2": {"requires": ["q1"]},
    "daily": {"daily": True},
}


def make_relay():
    relay = qsr.QuestShareRelay(
        quests=QUESTS,
        max_distance=10.0,
        request_ttl=30.0,
        period_seconds=100.0,
    )
    relay.add_character("leader", faction="A", phase=1, map_id="m1", x=0.0, y=0.0)
    relay.add_character("member", faction="A", phase=1, map_id="m1", x=3.0, y=4.0)
    relay.add_party("party", "leader", ["leader", "member"])
    return relay


def test_share_to_completed_target_is_rejected_without_resetting():
    relay = make_relay()
    relay.add_character("done", faction="A", phase=1, map_id="m1", x=1.0, y=1.0,
                        completed=["q1"])
    relay.add_party("party2", "leader", ["leader", "done"])
    result = relay.share("r1", "party2", "leader", "q1", now=0.0)
    assert result["accepted"] == []
    assert result["rejected"]["done"] == "completed"
    assert relay.progress("done", "q1")["progress"] == 0
    assert relay.log("done") == []


def test_share_to_already_active_target_is_rejected():
    relay = make_relay()
    first = relay.share("r1", "party", "leader", "q1", now=0.0)
    assert first["accepted"] == ["member"]
    assert relay.accept("r1", "member", now=1.0)["accepted"] is True
    second = relay.share("r2", "party", "leader", "q1", now=2.0)
    assert second["rejected"]["member"] == "already_active"
    assert len(relay.log("member")) == 1


def test_share_rejects_cross_faction_member():
    relay = make_relay()
    relay.add_character("enemy", faction="B", phase=1, map_id="m1", x=0.0, y=1.0)
    relay.add_party("party", "leader", ["leader", "enemy"])
    result = relay.share("r1", "party", "leader", "q1", now=0.0)
    assert result["rejected"]["enemy"] == "faction"
    assert result["accepted"] == []


def test_share_rejects_cross_phase_member():
    relay = make_relay()
    relay.add_character("phased", faction="A", phase=2, map_id="m1", x=0.0, y=1.0)
    relay.add_party("party", "leader", ["leader", "phased"])
    result = relay.share("r1", "party", "leader", "q1", now=0.0)
    assert result["rejected"]["phased"] == "phase"


def test_share_rejects_out_of_range_and_other_map_members():
    relay = make_relay()
    relay.add_character("far", faction="A", phase=1, map_id="m1", x=100.0, y=0.0)
    relay.add_character("other", faction="A", phase=1, map_id="m2", x=0.0, y=0.0)
    relay.add_party("party", "leader", ["leader", "far", "other"])
    result = relay.share("r1", "party", "leader", "q1", now=0.0)
    assert result["rejected"]["far"] == "distance"
    assert result["rejected"]["other"] == "map"


def test_share_requires_prerequisite_completion_per_member():
    relay = make_relay()
    relay.add_character("fresh", faction="A", phase=1, map_id="m1", x=0.0, y=1.0)
    relay.add_character("veteran", faction="A", phase=1, map_id="m1", x=0.0, y=2.0,
                        completed=["q1"])
    relay.add_party("party", "leader", ["leader", "fresh", "veteran"])
    result = relay.share("r1", "party", "leader", "q2", now=0.0)
    assert result["rejected"]["fresh"] == "prerequisite"
    assert result["accepted"] == ["veteran"]


def test_duplicate_share_request_id_is_idempotent():
    relay = make_relay()
    first = relay.share("r1", "party", "leader", "q1", now=0.0)
    second = relay.share("r1", "party", "leader", "q1", now=0.0)
    assert second["accepted"] == first["accepted"]
    assert relay.accept("r1", "member", now=1.0)["accepted"] is True
    repeat = relay.accept("r1", "member", now=2.0)
    assert repeat["accepted"] is False
    assert len(relay.log("member")) == 1


def test_leader_progress_and_member_progress_are_independent():
    relay = make_relay()
    relay.share("r1", "party", "leader", "q1", now=0.0)
    relay.accept("r1", "member", now=1.0)
    relay.add_progress("leader", "q1", 5, now=2.0)
    relay.add_progress("member", "q1", 3, now=3.0)
    assert relay.progress("leader", "q1")["progress"] == 5
    assert relay.progress("member", "q1")["progress"] == 3


def test_member_progress_events_accumulate_only_for_that_member():
    relay = make_relay()
    relay.share("r1", "party", "leader", "q1", now=0.0)
    relay.accept("r1", "member", now=1.0)
    relay.add_progress("member", "q1", 2, now=2.0)
    relay.add_progress("member", "q1", 3, now=3.0)
    relay.add_progress("leader", "q1", 2, now=4.0)
    assert relay.progress("member", "q1")["progress"] == 5
    assert relay.progress("leader", "q1")["progress"] == 2


def test_leader_change_does_not_touch_member_record():
    relay = make_relay()
    relay.add_character("member2", faction="A", phase=1, map_id="m1", x=1.0, y=0.0)
    relay.add_party("party", "leader", ["leader", "member", "member2"])
    relay.share("r1", "party", "leader", "q1", now=0.0)
    relay.accept("r1", "member", now=1.0)
    relay.add_progress("member", "q1", 4, now=2.0)
    relay.set_leader("party", "member2")
    record = relay.progress("member", "q1")
    assert record["progress"] == 4
    assert record["status"] == "active"
    assert len(relay.log("member")) == 1


def test_party_disband_invalidates_pending_share():
    relay = make_relay()
    relay.share("r1", "party", "leader", "q1", now=0.0)
    relay.disband("party")
    result = relay.accept("r1", "member", now=1.0)
    assert result["accepted"] is False
    assert result["reason"] == "party_disbanded"
    assert relay.log("member") == []


def test_share_request_expires_after_ttl():
    relay = make_relay()
    relay.add_character("member2", faction="A", phase=1, map_id="m1", x=1.0, y=0.0)
    relay.add_party("party", "leader", ["leader", "member", "member2"])
    relay.share("r1", "party", "leader", "q1", now=0.0)
    assert relay.accept("r1", "member", now=30.0)["accepted"] is True
    late = relay.accept("r1", "member2", now=31.0)
    assert late["accepted"] is False
    assert late["reason"] == "expired"


def test_abandon_clears_only_personal_progress():
    relay = make_relay()
    relay.share("r1", "party", "leader", "q1", now=0.0)
    relay.accept("r1", "member", now=1.0)
    relay.add_progress("leader", "q1", 5, now=2.0)
    relay.add_progress("member", "q1", 3, now=3.0)
    result = relay.abandon("member", "q1")
    assert result["abandoned"] is True
    assert relay.progress("member", "q1")["status"] == "not_active"
    assert relay.progress("leader", "q1")["progress"] == 5
    assert relay.log("member") == []


def test_daily_turn_in_is_counted_once_per_period_and_quest():
    relay = make_relay()
    relay.share("r1", "party", "leader", "daily", now=0.0)
    first = relay.turn_in("leader", "daily", now=0.0)
    assert first["accepted"] is True
    assert first["count"] == 1
    second = relay.turn_in("leader", "daily", now=50.0)
    assert second["accepted"] is False
    assert second["reason"] == "daily_limit"
    third = relay.turn_in("leader", "daily", now=100.0)
    assert third["accepted"] is True
    assert third["count"] == 1


def test_quest_log_is_deduplicated_and_keeps_share_source():
    relay = make_relay()
    relay.share("r1", "party", "leader", "q1", now=0.0)
    relay.accept("r1", "member", now=1.0)
    relay.add_progress("member", "q1", 2, now=2.0)
    relay.abandon("member", "q1")
    relay.share("r2", "party", "leader", "q1", now=3.0)
    assert relay.accept("r2", "member", now=4.0)["accepted"] is True
    relay.add_progress("member", "q1", 5, now=5.0)
    entries = relay.log("member")
    assert len(entries) == 1
    assert entries[0]["quest_id"] == "q1"
    assert entries[0]["progress"] == 5
    assert entries[0]["source"] == "r2"
