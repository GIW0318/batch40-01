import pytest

import transform_dirty_graph as tdg


def chain_graph():
    graph = tdg.TransformGraph()
    graph.add_node("root", translation=(0.0, 0.0, 0.0))
    graph.add_node("mid", parent="root", translation=(2.0, 0.0, 0.0))
    graph.add_node("leaf", parent="mid", translation=(3.0, 0.0, 0.0))
    return graph


def test_reparent_preserves_world_position():
    graph = chain_graph()
    assert graph.world_translation("leaf") == (5.0, 0.0, 0.0)
    graph.add_node("new_root", translation=(10.0, 0.0, 0.0))
    graph.reparent("leaf", "new_root")
    assert graph.world_translation("leaf") == pytest.approx((5.0, 0.0, 0.0))


def test_dirty_propagates_to_grandchildren():
    graph = chain_graph()
    graph.update()
    graph.mark_dirty("root")
    assert set(graph.update()) == {"root", "mid", "leaf"}


def test_update_recomputes_only_dirty_subtree():
    graph = chain_graph()
    graph.add_node("other", translation=(0.0, 5.0, 0.0))
    graph.update()
    graph.set_translation("leaf", (3.0, 1.0, 0.0))
    assert graph.update() == ["leaf"]
    assert graph.stats()["recomputed"] == 5


def test_leaf_change_does_not_touch_other_branch():
    graph = chain_graph()
    graph.add_node("other")
    graph.update()
    graph.set_translation("mid", (4.0, 0.0, 0.0))
    assert set(graph.dirty_nodes()) == {"mid", "leaf"}


def test_sibling_order_is_insertion_stable():
    graph = tdg.TransformGraph()
    for name in ["b", "a", "c"]:
        graph.add_node(name)
    assert graph.traversal_order() == ["b", "a", "c"]
    assert graph.serialize() == graph.serialize()


def test_serialization_roundtrip_preserves_parents():
    graph = chain_graph()
    text = graph.serialize()
    restored = tdg.TransformGraph.deserialize(text)
    assert restored.world_translation("leaf") == (5.0, 0.0, 0.0)
    assert restored.traversal_order() == ["root", "mid", "leaf"]


def test_hidden_parent_hides_descendants():
    graph = chain_graph()
    graph.set_visible("mid", False)
    assert graph.visible_nodes() == ["root"]


def test_instance_override_is_not_multiplied():
    graph = chain_graph()
    graph.add_node("weapon_proto", translation=(0.0, 2.0, 0.0))
    assert graph.instance_translation("weapon_proto", (0.0, 5.0, 0.0)) == (0.0, 5.0, 0.0)


def test_negative_scale_flips_normal():
    graph = chain_graph()
    graph.add_node("mirror", scale=(-1.0, 1.0, 1.0))
    assert graph.normal_sign("mirror") == -1.0
    assert graph.normal_sign("root") == 1.0


def test_traversal_snapshot_survives_mutation():
    graph = chain_graph()
    names = graph.iter_snapshot()
    for name in names:
        if name == "leaf":
            graph.remove_node("leaf")
    assert names == ["root", "mid", "leaf"]
    assert graph.traversal_order() == ["root", "mid"]


def test_paused_graph_skips_update():
    graph = chain_graph()
    graph.update()
    graph.set_paused(True)
    before = graph.stats()["recomputed"]
    graph.set_translation("leaf", (3.0, 0.0, 9.0))
    assert graph.update() == []
    assert graph.stats()["recomputed"] == before
    assert graph.updated_world("leaf") == (5.0, 0.0, 0.0)


def test_resume_after_pause_updates_dirty():
    graph = chain_graph()
    graph.update()
    graph.set_paused(True)
    graph.set_translation("leaf", (3.0, 0.0, 9.0))
    graph.update()
    graph.set_paused(False)
    assert graph.update() == ["leaf"]
    assert graph.updated_world("leaf") == (5.0, 0.0, 9.0)


def test_rebase_origin_preserves_relative_offsets():
    graph = tdg.TransformGraph()
    graph.add_node("origin", translation=(1000.0, 0.0, 0.0))
    graph.add_node("vehicle", parent="origin", translation=(3.0, 0.0, 0.0))
    assert graph.world_translation("vehicle") == (1003.0, 0.0, 0.0)
    graph.rebase_origin((1000.0, 0.0, 0.0))
    assert graph.world_translation("origin") == (0.0, 0.0, 0.0)
    assert graph.world_translation("vehicle") == (3.0, 0.0, 0.0)


def test_snapshot_is_isolated_from_later_writes():
    graph = chain_graph()
    snap = graph.snapshot()
    graph.set_translation("leaf", (3.0, 0.0, 7.0))
    assert snap["leaf"] == (5.0, 0.0, 0.0)
    assert graph.snapshot()["leaf"] == (5.0, 0.0, 7.0)


def test_cycle_reparent_is_rejected():
    graph = chain_graph()
    with pytest.raises(ValueError):
        graph.reparent("root", "leaf")
