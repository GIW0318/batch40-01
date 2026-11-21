import threading

import pytest

import collision_layer_matrix as clm


def test_forty_layers_do_not_wrap_the_mask():
    matrix = clm.CollisionLayerMatrix(layer_count=40)
    matrix.set_collision(35, 3)
    assert matrix.collides(35, 3) is True
    assert matrix.collides(39, 3) is False
    assert matrix.mask_for(35) == 1 << 35
    assert matrix.mask_for(35) & matrix.mask_for(3) == 0


def test_masks_continue_beyond_sixty_four_layers():
    matrix = clm.CollisionLayerMatrix(layer_count=100)
    matrix.set_collision(63, 64)
    assert matrix.collides(63, 64) is True
    assert matrix.mask_for(64) == 1 << 64


def test_collision_matrix_is_symmetric():
    matrix = clm.CollisionLayerMatrix()
    matrix.set_collision(1, 2)
    assert matrix.collides(1, 2) is True
    assert matrix.collides(2, 1) is True
    matrix.set_collision(1, 2, False)
    assert matrix.collides(1, 2) is False
    assert matrix.collides(2, 1) is False


def test_query_mask_filters_bodies():
    matrix = clm.CollisionLayerMatrix(layer_count=40)
    matrix.add_body("a", 5)
    matrix.add_body("b", 6)
    assert matrix.query_bodies(1 << 5) == ["a"]
    assert matrix.query_bodies((1 << 5) | (1 << 6)) == ["a", "b"]


def test_disabled_body_is_not_queryable():
    matrix = clm.CollisionLayerMatrix()
    matrix.add_body("wall", 7, enabled=False)
    matrix.add_body("floor", 7)
    assert matrix.query_bodies(1 << 7) == ["floor"]


def test_trigger_matrix_is_separate_from_solid_matrix():
    matrix = clm.CollisionLayerMatrix()
    matrix.set_trigger(3, 4)
    assert matrix.trigger_collides(3, 4) is True
    assert matrix.trigger_collides(4, 3) is True
    assert matrix.collides(3, 4) is False


def test_solid_and_trigger_queries_use_different_pools():
    matrix = clm.CollisionLayerMatrix()
    matrix.add_body("trigger", 3, trigger=True)
    matrix.add_body("solid", 3)
    assert matrix.query_bodies(1 << 3) == ["solid"]
    assert matrix.query_bodies(1 << 3, trigger=True) == ["trigger"]


def test_compound_child_inherits_and_follows_parent_layer():
    matrix = clm.CollisionLayerMatrix()
    matrix.add_body("player", 5)
    matrix.add_child("player", "cloak")
    assert matrix.query_bodies(1 << 5) == ["cloak", "player"]
    matrix.set_body_layer("player", 7)
    assert matrix.query_bodies(1 << 5) == []
    assert matrix.query_bodies(1 << 7) == ["cloak", "player"]


def test_layer_rename_keeps_serialized_id():
    matrix = clm.CollisionLayerMatrix()
    matrix.define_layer("Enemy", 5)
    matrix.set_collision(5, 6)
    matrix.rename_layer(5, "Monster")
    assert matrix.layer_id("Monster") == 5
    assert matrix.name_of(5) == "Monster"
    data = matrix.serialize()
    restored = clm.CollisionLayerMatrix()
    restored.load(data)
    assert restored.layer_id("Monster") == 5
    assert restored.collides(5, 6) is True


def test_runtime_layer_change_requires_rebuild():
    matrix = clm.CollisionLayerMatrix()
    matrix.add_body("a", 1)
    matrix.add_body("b", 2)
    matrix.set_collision(1, 2)
    matrix.rebuild()
    assert matrix.pairs() == [("a", "b")]
    matrix.set_body_layer("a", 3)
    assert matrix.dirty is True
    assert matrix.pairs() == [("a", "b")]
    matrix.rebuild()
    assert matrix.pairs() == []


def test_disabling_body_removes_its_pairs_after_rebuild():
    matrix = clm.CollisionLayerMatrix()
    matrix.add_body("a", 1)
    matrix.add_body("b", 2)
    matrix.set_collision(1, 2)
    matrix.rebuild()
    assert matrix.pairs() == [("a", "b")]
    matrix.disable_body("b")
    matrix.rebuild()
    assert matrix.pairs() == []


def test_layer_count_above_maximum_is_rejected():
    with pytest.raises(ValueError):
        clm.CollisionLayerMatrix(layer_count=300)


def test_invalid_layer_indices_are_rejected():
    matrix = clm.CollisionLayerMatrix(layer_count=40)
    with pytest.raises(ValueError):
        matrix.collides(-1, 0)
    with pytest.raises(ValueError):
        matrix.collides(0, 40)
    with pytest.raises(ValueError):
        matrix.mask_for(40)


def test_empty_or_duplicate_layer_names_are_rejected():
    matrix = clm.CollisionLayerMatrix()
    with pytest.raises(ValueError):
        matrix.define_layer("", 4)
    matrix.define_layer("A", 3)
    with pytest.raises(ValueError):
        matrix.define_layer("B", 3)
    with pytest.raises(ValueError):
        matrix.define_layer("A", 4)


def test_concurrent_layer_reads_do_not_see_partial_masks():
    matrix = clm.CollisionLayerMatrix()
    matrix.set_collision(1, 2)
    matrix.set_collision(4, 5)
    errors = []

    def writer():
        try:
            for i in range(200):
                matrix.set_collision(1, 2, i % 2 == 0)
        except Exception as exc:
            errors.append(exc)

    def reader():
        try:
            for _ in range(200):
                assert matrix.collides(4, 5) is True
                matrix.serialize()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert matrix.collides(4, 5) is True
