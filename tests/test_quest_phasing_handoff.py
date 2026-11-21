import pytest

import quest_phasing_handoff as qph


def make_phases():
    return {
        1: {"generation": 1, "npcs": ["giver", "turnin", "vendor"], "locked": False},
        2: {"generation": 1, "npcs": ["shadow", "vendor"], "locked": False},
        3: {"generation": 1, "npcs": [], "locked": True},
        9: {"generation": 1, "npcs": [], "transitional": True},
    }


def make_npcs():
    return {
        "giver": {"phase": 1, "quest": "q1", "stages": ["available"]},
        "turnin": {"phase": 1, "quest": "q1", "stages": ["ready"]},
        "vendor": {"phase": 1},
        "shadow": {"phase": 2},
    }


QUESTS = {"q1": {"requires": []}, "q2": {"requires": ["q1"]}}


def make_handler():
    handler = qph.QuestPhasingHandoff(phases=make_phases(), npcs=make_npcs(), quests=QUESTS)
    handler.add_character("c1", phase=0, quests={"q1": {"stage": "available", "progress": 4}})
    return handler


def test_enter_phase_keeps_personal_quest_progress():
    handler = make_handler()
    result = handler.enter_phase("c1", 1)
    assert result["ok"] is True
    snapshot = handler.sync("c1")
    assert snapshot["phase_id"] == 1
    assert snapshot["quests"]["q1"]["progress"] == 4
    assert snapshot["quests"]["q1"]["stage"] == "available"


def test_leave_phase_clears_markers_but_keeps_progress():
    handler = make_handler()
    handler.enter_phase("c1", 1)
    handler.mark("c1", "herb_collected")
    handler.leave_phase("c1")
    snapshot = handler.sync("c1")
    assert snapshot["phase_id"] == 0
    assert snapshot["phase_markers"] == []
    assert snapshot["quests"]["q1"]["progress"] == 4


def test_npc_visibility_is_scoped_to_current_phase():
    handler = make_handler()
    handler.enter_phase("c1", 1)
    assert handler.npc_state("c1", "giver")["visible"] is True
    assert handler.npc_state("c1", "shadow")["visible"] is False
    handler.enter_phase("c1", 2)
    assert handler.npc_state("c1", "shadow")["visible"] is True
    assert handler.npc_state("c1", "giver")["visible"] is False


def test_two_characters_in_same_phase_see_the_same_npc_state():
    handler = make_handler()
    handler.add_character("c2", phase=0, quests={"q1": {"stage": "available", "progress": 0}})
    handler.enter_phase("c1", 1)
    handler.enter_phase("c2", 1)
    for npc_id in ("giver", "turnin", "vendor"):
        assert handler.npc_state("c1", npc_id) == handler.npc_state("c2", npc_id)


def test_quest_stage_change_only_affects_that_character_view():
    handler = make_handler()
    handler.add_character("c2", phase=0, quests={"q1": {"stage": "available", "progress": 0}})
    handler.enter_phase("c1", 1)
    handler.enter_phase("c2", 1)
    handler.set_quest_stage("c1", "q1", "ready", progress=100)
    assert handler.npc_state("c1", "turnin")["visible"] is True
    assert handler.npc_state("c1", "giver")["visible"] is False
    assert handler.npc_state("c2", "giver")["visible"] is True
    assert handler.npc_state("c2", "turnin")["visible"] is False


def test_completed_quest_hides_turn_in_npc():
    handler = make_handler()
    handler.enter_phase("c1", 1)
    assert handler.npc_state("c1", "turnin")["visible"] is False
    handler.set_quest_stage("c1", "q1", "ready", progress=100)
    assert handler.npc_state("c1", "turnin")["visible"] is True
    handler.set_quest_stage("c1", "q1", "done", progress=100)
    assert handler.npc_state("c1", "turnin")["visible"] is False
    assert handler.npc_state("c1", "giver")["visible"] is False


def test_locked_phase_entry_rolls_back_phase_and_quest_state():
    handler = make_handler()
    handler.enter_phase("c1", 1)
    handler.set_quest_stage("c1", "q1", "ready", progress=7)
    result = handler.enter_phase("c1", 3)
    assert result["ok"] is False
    assert result["reason"] == "locked"
    snapshot = handler.sync("c1")
    assert snapshot["phase_id"] == 1
    assert snapshot["quests"]["q1"]["stage"] == "ready"
    assert snapshot["quests"]["q1"]["progress"] == 7


def test_transitional_phase_has_no_quest_logic_and_blocks_instances():
    handler = make_handler()
    handler.enter_phase("c1", 9)
    assert handler.npc_list("c1") == []
    instance = handler.enter_instance("c1", "inst-1")
    assert instance["ok"] is False
    assert instance["reason"] == "transitional_phase"


def test_phase_transition_and_instance_entry_are_mutually_exclusive():
    handler = make_handler()
    handler.enter_phase("c1", 1)
    assert handler.enter_instance("c1", "inst-1")["ok"] is True
    blocked = handler.enter_phase("c1", 2)
    assert blocked["ok"] is False
    assert blocked["reason"] == "instance_active"
    handler.leave_instance("c1")
    assert handler.enter_phase("c1", 2)["ok"] is True


def test_merge_phases_deduplicates_npcs_by_id():
    handler = make_handler()
    result = handler.merge_phases(1, 2)
    assert result["merged"] is True
    merged = handler.phase_npcs(2)
    assert sorted(merged) == ["giver", "shadow", "turnin", "vendor"]
    assert len(merged) == len(set(merged))


def test_merge_phases_moves_characters_and_keeps_progress():
    handler = make_handler()
    handler.enter_phase("c1", 1)
    handler.set_quest_stage("c1", "q1", "ready", progress=9)
    handler.merge_phases(1, 2)
    snapshot = handler.sync("c1")
    assert snapshot["phase_id"] == 2
    assert snapshot["quests"]["q1"]["progress"] == 9
    assert snapshot["quests"]["q1"]["stage"] == "ready"
    assert handler.npc_state("c1", "vendor")["visible"] is True


def test_generation_bump_discards_stale_phase_markers_but_keeps_progress():
    handler = make_handler()
    handler.enter_phase("c1", 1)
    handler.mark("c1", "herb_collected")
    handler.leave_phase("c1")
    handler.enter_phase("c1", 1)
    assert handler.sync("c1")["phase_markers"] == ["herb_collected"]
    handler.leave_phase("c1")
    handler.bump_generation(1)
    handler.enter_phase("c1", 1)
    snapshot = handler.sync("c1")
    assert snapshot["phase_markers"] == []
    assert snapshot["quests"]["q1"]["progress"] == 4
    assert snapshot["generation"] == 2


def test_npc_state_reports_current_phase_and_generation():
    handler = make_handler()
    handler.enter_phase("c1", 1)
    state = handler.npc_state("c1", "giver")
    assert state["phase_id"] == 1
    assert state["generation"] == 1
    handler.leave_phase("c1")
    handler.bump_generation(1)
    handler.enter_phase("c1", 1)
    state = handler.npc_state("c1", "giver")
    assert state["phase_id"] == 1
    assert state["generation"] == 2


def test_sync_snapshot_restores_after_restart():
    handler = make_handler()
    handler.enter_phase("c1", 1)
    handler.mark("c1", "npc_talked")
    handler.set_quest_stage("c1", "q1", "ready", progress=11)
    snapshot = handler.sync("c1")
    restarted = qph.QuestPhasingHandoff(phases=make_phases(), npcs=make_npcs(), quests=QUESTS)
    restarted.restore(snapshot)
    restored = restarted.sync("c1")
    assert restored["phase_id"] == 1
    assert restored["phase_markers"] == ["npc_talked"]
    assert restored["quests"]["q1"]["progress"] == 11
    assert restored["quests"]["q1"]["stage"] == "ready"
    assert restarted.npc_state("c1", "turnin")["visible"] is True


def test_cross_phase_share_fails_with_explicit_reason():
    handler = make_handler()
    handler.add_character("c2", phase=2, quests={})
    failed = handler.share_quest("c1", "c2", "q1")
    assert failed["shared"] is False
    assert failed["reason"] == "phase"
    handler.enter_phase("c1", 2)
    assert handler.share_quest("c1", "c2", "q1")["shared"] is True
    assert handler.sync("c2")["quests"]["q1"]["stage"] == "available"
