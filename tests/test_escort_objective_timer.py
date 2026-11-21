import pytest

import escort_objective_timer as eot


ROUTE = {
    "npc_id": "caravan",
    "checkpoints": ["cp1", "cp2", "cp3"],
    "time_limit": 60.0,
    "positions": {"cp1": (0.0, 0.0), "cp2": (10.0, 0.0), "cp3": (20.0, 0.0)},
}


def make_timer():
    return eot.EscortObjectiveTimer(route=ROUTE, pause_range=15.0, grace=20.0)


def test_start_is_idempotent_per_party_and_npc_is_exclusive():
    timer = make_timer()
    first = timer.start("party", members=["a", "b"], now=0.0)
    second = timer.start("party", members=["a", "b"], now=1.0)
    assert first["instance_id"] == second["instance_id"]
    with pytest.raises(ValueError):
        timer.start("other", members=["c"], now=2.0)


def test_checkpoints_must_be_entered_in_order():
    timer = make_timer()
    timer.start("party", members=["a"], now=0.0)
    with pytest.raises(ValueError):
        timer.checkpoint("party", "cp2", position=(10.0, 0.0))
    timer.checkpoint("party", "cp1", position=(0.0, 0.0))
    with pytest.raises(ValueError):
        timer.checkpoint("party", "cp3", position=(20.0, 0.0))


def test_checkpoint_progress_is_stored_separately_from_completion():
    timer = make_timer()
    timer.start("party", members=["a"], now=0.0)
    timer.checkpoint("party", "cp1", position=(0.0, 0.0))
    status = timer.status("party")
    assert status["checkpoints_done"] == ["cp1"]
    assert status["checkpoint_index"] == 1
    assert status["state"] == "running"


def test_complete_before_all_checkpoints_is_rejected_then_succeeds():
    timer = make_timer()
    timer.start("party", members=["a"], now=0.0)
    early = timer.complete("party", now=1.0)
    assert early["ok"] is False
    assert early["reason"] == "checkpoints_incomplete"
    timer.checkpoint("party", "cp1", position=(0.0, 0.0))
    timer.checkpoint("party", "cp2", position=(10.0, 0.0))
    timer.checkpoint("party", "cp3", position=(20.0, 0.0))
    done = timer.complete("party", now=2.0)
    assert done["ok"] is True
    assert done["state"] == "completed"


def test_duplicate_complete_settles_reward_only_once_and_fail_is_ignored():
    timer = make_timer()
    timer.start("party", members=["a"], now=0.0)
    timer.checkpoint("party", "cp1", position=(0.0, 0.0))
    timer.checkpoint("party", "cp2", position=(10.0, 0.0))
    timer.checkpoint("party", "cp3", position=(20.0, 0.0))
    first = timer.complete("party", now=2.0)
    second = timer.complete("party", now=3.0)
    late_fail = timer.fail("party", "timeout", now=4.0)
    assert first["reward_issued"] is True
    assert second["reward_issued"] is False
    assert second["duplicate"] is True
    assert late_fail["ok"] is False
    assert timer.status("party")["reward_count"] == 1
    assert timer.status("party")["state"] == "completed"


def test_timeout_fails_when_elapsed_exceeds_limit():
    timer = make_timer()
    timer.start("party", members=["a"], now=0.0)
    timer.advance("party", (0.0, 0.0), now=61.0, npc_position=(0.0, 0.0))
    status = timer.status("party")
    assert status["state"] == "failed"
    assert status["reason"] == "timeout"
    assert status["elapsed"] > 60.0


def test_complete_at_exact_deadline_is_a_success():
    timer = make_timer()
    timer.start("party", members=["a"], now=0.0)
    timer.advance("party", (0.0, 0.0), now=60.0, npc_position=(0.0, 0.0))
    timer.checkpoint("party", "cp1", position=(0.0, 0.0))
    timer.checkpoint("party", "cp2", position=(10.0, 0.0))
    timer.checkpoint("party", "cp3", position=(20.0, 0.0))
    result = timer.complete("party", now=60.0)
    assert result["ok"] is True
    assert timer.status("party")["state"] == "completed"


def test_party_out_of_range_pauses_instead_of_failing():
    timer = make_timer()
    timer.start("party", members=["a"], now=0.0)
    timer.advance("party", (0.0, 0.0), now=10.0, npc_position=(0.0, 0.0))
    paused = timer.advance("party", (100.0, 0.0), now=20.0, npc_position=(0.0, 0.0))
    assert paused["state"] == "paused"
    assert paused["reason"] == "out_of_range"
    assert paused["elapsed"] == 10.0
    later = timer.advance("party", (120.0, 0.0), now=50.0, npc_position=(0.0, 0.0))
    assert later["state"] == "paused"
    assert later["elapsed"] == 10.0


def test_resume_continues_from_the_pause_point():
    timer = make_timer()
    timer.start("party", members=["a"], now=0.0)
    timer.advance("party", (0.0, 0.0), now=10.0, npc_position=(0.0, 0.0))
    timer.advance("party", (100.0, 0.0), now=20.0, npc_position=(0.0, 0.0))
    resumed = timer.advance("party", (0.0, 0.0), now=50.0, npc_position=(0.0, 0.0))
    assert resumed["state"] == "running"
    assert resumed["elapsed"] == 10.0
    advanced = timer.advance("party", (0.0, 0.0), now=60.0, npc_position=(0.0, 0.0))
    assert advanced["elapsed"] == 20.0


def test_blocked_npc_pauses_the_timer():
    timer = make_timer()
    timer.start("party", members=["a"], now=0.0)
    paused = timer.advance("party", (0.0, 0.0), now=10.0,
                           npc_position=(0.0, 0.0), blocked=True)
    assert paused["state"] == "paused"
    assert paused["reason"] == "npc_blocked"
    assert paused["elapsed"] == 0.0
    assert paused["remaining"] == 60.0


def test_zero_npc_health_fails_the_escort():
    timer = make_timer()
    timer.start("party", members=["a"], now=0.0)
    result = timer.advance("party", (0.0, 0.0), now=5.0,
                           npc_position=(0.0, 0.0), npc_hp=0.0)
    assert result["state"] == "failed"
    assert result["reason"] == "npc_dead"


def test_disconnect_grace_keeps_instance_and_reconnect_returns_to_it():
    timer = make_timer()
    started = timer.start("party", members=["a", "b"], now=0.0)
    status = timer.advance("party", (0.0, 0.0), now=5.0,
                           npc_position=(0.0, 0.0), members=["a"])
    assert status["state"] == "running"
    assert status["disconnected"] == ["b"]
    assert status["instance_id"] == started["instance_id"]
    reconnected = timer.reconnect("party", "b", now=6.0)
    assert reconnected["instance_id"] == started["instance_id"]
    assert reconnected["disconnected"] == []


def test_mid_join_member_receives_current_checkpoint_progress():
    timer = make_timer()
    timer.start("party", members=["a"], now=0.0)
    timer.checkpoint("party", "cp1", position=(0.0, 0.0))
    timer.advance("party", (0.0, 0.0), now=2.0, npc_position=(0.0, 0.0),
                  members=["a", "late"])
    status = timer.status("party")
    assert status["member_progress"]["a"] == 1
    assert status["member_progress"]["late"] == 1


def test_abandon_recycles_the_npc_and_next_start_is_fresh():
    timer = make_timer()
    first = timer.start("party", members=["a"], now=0.0)
    timer.advance("party", (0.0, 0.0), now=10.0, npc_position=(0.0, 0.0), npc_hp=40.0)
    abandoned = timer.abandon("party", now=11.0)
    assert abandoned["state"] == "abandoned"
    other = timer.start("other", members=["b"], now=12.0)
    assert other["instance_id"] != first["instance_id"]
    assert other["npc_hp"] == 100.0
    assert other["elapsed"] == 0.0
    assert other["checkpoint_index"] == 0


def test_snapshot_and_restore_preserve_used_time_not_wall_clock():
    timer = make_timer()
    timer.start("party", members=["a"], now=0.0)
    timer.advance("party", (0.0, 0.0), now=30.0, npc_position=(0.0, 0.0))
    snapshot = timer.snapshot("party")
    restarted = make_timer()
    restarted.restore(snapshot, now=100.0)
    assert restarted.status("party")["elapsed"] == 30.0
    advanced = restarted.advance("party", (0.0, 0.0), now=110.0,
                                 npc_position=(0.0, 0.0))
    assert advanced["elapsed"] == 40.0
