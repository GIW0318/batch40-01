import pytest

import anim_graph_transition as agt


def service(max_queue=4):
    graph = agt.AnimGraphTransition(max_queue=max_queue)
    graph.add_state("light", 1.0)
    graph.add_state("heavy", 1.0)
    graph.add_state("dodge", 1.0)
    return graph


def test_interrupt_continues_phase_instead_of_resetting():
    graph = service()
    graph.play("light")
    graph.transition_to("heavy", duration=1.0)
    graph.update(0.4)
    result = graph.transition_to("dodge", duration=1.0, interrupt=True)
    assert result["source"] == "heavy"
    assert result["source_phase"] == pytest.approx(0.4)
    assert result["queued"] is False


def test_transition_uses_current_source_phase():
    graph = service()
    graph.play("light", phase=0.6)
    result = graph.transition_to("heavy", duration=1.0)
    assert result["source"] == "light"
    assert result["target"] == "heavy"
    assert result["source_phase"] == pytest.approx(0.6)


def test_target_is_queued_when_not_interrupting():
    graph = service()
    graph.play("light")
    graph.transition_to("heavy", duration=1.0)
    result = graph.transition_to("dodge")
    assert result["queued"] is True
    assert graph.queue_size() == 1


def test_duplicate_queue_request_is_ignored():
    graph = service()
    graph.play("light")
    graph.transition_to("heavy", duration=1.0)
    graph.transition_to("dodge")
    result = graph.transition_to("dodge")
    assert result["queued"] is False
    assert graph.queue_size() == 1


def test_queue_is_capped():
    graph = agt.AnimGraphTransition(max_queue=2)
    for name in ["a", "b", "c", "d", "e"]:
        graph.add_state(name, 1.0)
    graph.play("a")
    graph.transition_to("b", duration=1.0)
    assert graph.transition_to("c")["queued"] is True
    assert graph.transition_to("d")["queued"] is True
    assert graph.transition_to("e")["queued"] is False
    assert graph.queue_size() == 2


def test_remap_phase_uses_target_duration():
    graph = service()
    assert graph.remap_phase(0.9, 10.0, 2.0) == pytest.approx(1.8)
    assert graph.remap_phase(0.5, 2.0, 2.0) == pytest.approx(1.0)


def test_event_is_reported_once_across_both_ends():
    graph = agt.AnimGraphTransition()
    graph.add_state("a", 1.0, events=[(0.75, "damage")])
    graph.add_state("b", 1.0, events=[(0.25, "damage")])
    graph.play("a", phase=0.5)
    graph.transition_to("b", duration=1.0)
    result = graph.update(0.5)
    assert result["events"].count("damage") == 1


def test_event_is_not_fired_twice():
    graph = agt.AnimGraphTransition()
    graph.add_state("a", 1.0, events=[(0.9, "swing")])
    graph.add_state("b", 1.0)
    graph.play("a", phase=0.5)
    graph.transition_to("b", duration=1.0)
    assert "swing" not in graph.update(0.3)["events"]
    assert "swing" in graph.update(0.5)["events"]
    assert "swing" not in graph.update(0.2)["events"]


def test_zero_duration_transition_completes_immediately():
    graph = service()
    graph.play("light")
    graph.transition_to("heavy", duration=0.0)
    result = graph.update(0.0)
    assert result["state"] == "heavy"
    assert result["weight"] == pytest.approx(1.0)
    assert result["transition_active"] is False


def test_blend_weight_handles_zero_duration():
    graph = service()
    assert graph.blend_weight_at(0.0, 0.0) == pytest.approx(1.0)
    assert graph.blend_weight_at(0.0, 5.0) == pytest.approx(1.0)
    assert graph.blend_weight_at(1.0, 0.5) == pytest.approx(0.5)


def test_upper_mask_excludes_finger_bones():
    graph = service()
    assert graph.bone_allowed("upper", "finger_index") is False
    assert graph.bone_allowed("upper", "finger_thumb") is False


def test_upper_mask_allows_arm_and_full_mask_allows_fingers():
    graph = service()
    assert graph.bone_allowed("upper", "hand") is True
    assert graph.bone_allowed("upper", "arm") is True
    assert graph.bone_allowed("full", "finger_index") is True


def test_root_motion_is_weighted_not_additive():
    graph = service()
    assert graph.blend_root_motion(
        [2.0, 4.0], [0.5, 0.5]
    ) == pytest.approx(3.0)
    assert graph.blend_root_motion(
        [2.0, 4.0], [0.25, 0.75]
    ) == pytest.approx(3.5)


def test_layer_weights_are_normalized():
    graph = service()
    weights = graph.combine_layer_weights([("base", 0.6), ("upper", 0.6)])
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["base"] == pytest.approx(0.5)
    assert weights["upper"] == pytest.approx(0.5)


def test_hot_reload_invalidates_active_transition():
    graph = service()
    graph.play("light")
    graph.transition_to("heavy", duration=1.0)
    graph.update(0.1)
    graph.hot_reload()
    with pytest.raises(RuntimeError):
        graph.update(0.1)
    graph.transition_to("heavy", duration=1.0)
    assert graph.update(0.1)["transition_active"] is True
