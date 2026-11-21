import pytest

import bone_palette_ring as bpr


def build_palette():
    pal = bpr.BonePalette(max_bones=256, frames_in_flight=2)
    root = pal.add_bone("root")
    upper = pal.add_bone("upper_arm", parent="root")
    hand = pal.add_bone("hand", parent="upper_arm")
    return pal, root, upper, hand


def test_dirty_mark_propagates_to_entire_subtree():
    pal, root, upper, hand = build_palette()
    pal.submit(1)
    pal.on_gpu_consumed(1)
    pal.set_local_translation("upper_arm", (0.0, 1.0, 0.0))
    assert pal.dirty_bones() == ["upper_arm", "hand"]


def test_leaf_change_does_not_dirty_ancestors():
    pal, root, upper, hand = build_palette()
    pal.submit(1)
    pal.on_gpu_consumed(1)
    pal.set_local_translation("hand", (0.0, 0.0, 2.0))
    assert pal.dirty_bones() == ["hand"]


def test_world_translation_accumulates_parent_chain():
    pal, root, upper, hand = build_palette()
    pal.set_local_translation("root", (1.0, 0.0, 0.0))
    pal.set_local_translation("upper_arm", (0.0, 2.0, 0.0))
    pal.set_local_translation("hand", (0.0, 0.0, 3.0))
    assert pal.world_translation("hand") == (1.0, 2.0, 3.0)


def test_root_motion_is_applied_once():
    pal, root, upper, hand = build_palette()
    pal.set_local_translation("root", (1.0, 0.0, 0.0))
    pal.set_local_translation("upper_arm", (0.0, 1.0, 0.0))
    pal.set_root_motion((10.0, 0.0, 0.0))
    assert pal.world_translation("root") == (11.0, 0.0, 0.0)
    assert pal.world_translation("upper_arm") == (11.0, 1.0, 0.0)


def test_submit_uploads_only_dirty_bones():
    pal = bpr.BonePalette()
    for index in range(5):
        pal.add_bone("b%d" % index)
    assert pal.submit(1) == [0, 1, 2, 3, 4]
    pal.on_gpu_consumed(1)
    pal.set_local_translation("b3", (0.0, 0.0, 1.0))
    assert pal.submit(2) == [3]


def test_in_flight_limit_blocks_frame_overwrite():
    pal, root, upper, hand = build_palette()
    assert len(pal.submit(1)) == 3
    pal.set_local_translation("hand", (1.0, 0.0, 0.0))
    assert pal.submit(2) == [2]
    pal.set_local_translation("hand", (2.0, 0.0, 0.0))
    assert pal.submit(3) == []
    pal.on_gpu_consumed(1)
    assert pal.submit(3) == [2]


def test_barrier_required_until_gpu_consumes():
    pal, root, upper, hand = build_palette()
    pal.set_local_translation("hand", (1.0, 0.0, 0.0))
    pal.submit(1)
    assert pal.requires_barrier() is True
    pal.set_local_translation("hand", (2.0, 0.0, 0.0))
    pal.submit(2)
    pal.on_gpu_consumed(1)
    assert pal.requires_barrier() is True
    pal.on_gpu_consumed(2)
    assert pal.requires_barrier() is False


def test_paused_palette_skips_upload():
    pal, root, upper, hand = build_palette()
    pal.set_paused(True)
    assert pal.submit(1) == []
    assert pal.stats()["uploaded"] == 0
    assert pal.dirty_bones() != []


def test_resume_uploads_pending_dirty_bones():
    pal, root, upper, hand = build_palette()
    pal.set_paused(True)
    pal.submit(1)
    pal.set_paused(False)
    assert len(pal.submit(2)) == 3
    assert pal.stats()["uploaded"] == 3


def test_bone_index_overflow_is_rejected():
    pal = bpr.BonePalette(max_bones=2)
    assert pal.add_bone("a") == 0
    assert pal.add_bone("b") == 1
    with pytest.raises(ValueError):
        pal.add_bone("c")


def test_unknown_bone_lookup_raises_keyerror():
    pal, root, upper, hand = build_palette()
    with pytest.raises(KeyError):
        pal.world_translation("ghost")


def test_bound_material_blocks_bone_removal():
    pal, root, upper, hand = build_palette()
    pal.bind_material("steel", "hand")
    with pytest.raises(ValueError):
        pal.remove_bone("hand")
    pal.release_material("steel")
    pal.remove_bone("hand")
    with pytest.raises(KeyError):
        pal.world_translation("hand")


def test_bind_inverse_recomputed_after_import_scale():
    pal = bpr.BonePalette()
    pal.add_bone("spine", bind=(2.0, 0.0, 0.0))
    assert pal.bind_inverse_translation("spine") == (-2.0, 0.0, 0.0)
    pal.apply_import_scale(2.0)
    assert pal.bind_inverse_translation("spine") == (-4.0, 0.0, 0.0)


def test_palette_snapshot_stable_for_second_camera():
    pal, root, upper, hand = build_palette()
    pal.set_local_translation("hand", (0.0, 0.0, 1.0))
    pal.submit(1)
    pal.set_local_translation("hand", (0.0, 0.0, 4.0))
    pal.submit(2)
    assert pal.palette_for_frame(1)["hand"] == (0.0, 0.0, 1.0)
    assert pal.palette_for_frame(2)["hand"] == (0.0, 0.0, 4.0)


def test_instance_palette_bases_do_not_overlap():
    allocator = bpr.InstancePaletteAllocator(bone_count=256)
    first = allocator.allocate("guard_a")
    second = allocator.allocate("guard_b")
    assert first == 0
    assert second == 256
    assert first != second
    allocator.release("guard_a")
    with pytest.raises(KeyError):
        allocator.base_of("guard_a")
