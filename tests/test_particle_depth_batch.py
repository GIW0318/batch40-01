import pytest

import particle_depth_batch as pdb


def part(pid, z, size=1.0, alpha=1.0, emitter="e"):
    return {
        "id": pid,
        "pos": (0.0, 0.0, z),
        "size": size,
        "alpha": alpha,
        "emitter": emitter,
    }


def service():
    return pdb.ParticleDepthBatch(sort_budget_ms=1.0)


def test_sort_is_global_across_emitters():
    batch = service()
    particles = [
        part(1, 10.0, emitter="a"),
        part(2, 30.0, emitter="b"),
        part(3, 20.0, emitter="a"),
    ]
    result = batch.sort_particles(particles, (0.0, 0.0, 100.0))
    assert [p["id"] for p in result["order"]] == [1, 3, 2]


def test_sort_is_back_to_front():
    batch = service()
    particles = [part(1, 10.0), part(2, 30.0), part(3, 20.0)]
    result = batch.sort_particles(particles, (0.0, 0.0, 100.0))
    depths = [batch.depth(p, (0.0, 0.0, 100.0)) for p in result["order"]]
    assert depths == sorted(depths, reverse=True)
    assert result["sorted"] is True


def test_depth_follows_camera_movement():
    batch = service()
    particles = [part(1, 10.0), part(2, 30.0)]
    near_camera = batch.sort_particles(particles, (0.0, 0.0, 100.0))
    far_camera = batch.sort_particles(particles, (0.0, 0.0, 0.0))
    near_depths = [
        batch.depth(p, (0.0, 0.0, 100.0)) for p in near_camera["order"]
    ]
    far_depths = [
        batch.depth(p, (0.0, 0.0, 0.0)) for p in far_camera["order"]
    ]
    assert near_depths == pytest.approx([89.5, 69.5])
    assert far_depths == pytest.approx([-10.5, -30.5])


def test_depth_uses_bounding_sphere_nearest_point():
    batch = service()
    small = part(1, 10.0, size=2.0)
    large = part(2, 10.0, size=10.0)
    result = batch.sort_particles([large, small], (0.0, 0.0, 100.0))
    assert [p["id"] for p in result["order"]] == [1, 2]


def test_visible_uses_bounding_extent_at_near_plane():
    batch = service()
    assert batch.visible(part(1, 0.05, size=0.2), 0.1) is True


def test_particle_fully_behind_near_plane_is_invisible():
    batch = service()
    assert batch.visible(part(1, -1.0, size=0.1), 0.1) is False


def test_equal_depths_are_ordered_by_id():
    batch = service()
    particles = [part(5, 10.0), part(2, 10.0)]
    result = batch.sort_particles(particles, (0.0, 0.0, 100.0))
    assert [p["id"] for p in result["order"]] == [2, 5]


def test_generate_chunks_is_deterministic():
    batch = service()
    first = [{"id": 3}, {"id": 1}]
    second = [{"id": 2}]
    assert [p["id"] for p in batch.generate_chunks([first, second])] == [
        1,
        2,
        3,
    ]
    assert [p["id"] for p in batch.generate_chunks([second, first])] == [
        1,
        2,
        3,
    ]


def test_pool_release_clears_stale_sort_index():
    batch = service()
    particle = batch.acquire("fire")
    assert particle["sort_index"] is None
    assert particle["alive"] is False
    particle["sort_index"] = 7
    batch.release(particle)
    reused = batch.acquire("fire")
    assert reused["sort_index"] is None
    assert reused["alive"] is False


def test_index_bits_grow_above_16_bits():
    batch = service()
    assert batch.required_index_bits(65536) == 16
    assert batch.required_index_bits(70000) == 17


def test_encode_index_never_wraps():
    batch = service()
    assert batch.encode_index(70000, 17) == 70000
    with pytest.raises(OverflowError):
        batch.encode_index(70000, 16)


def test_sort_within_budget_returns_sorted_order():
    batch = service()
    particles = [part(index, float(9 - index)) for index in range(10)]
    result = batch.sort_particles(
        particles, (0.0, 0.0, 100.0), budget_ms=1.0
    )
    assert result["sorted"] is True
    assert result["budget_exceeded"] is False
    assert [p["id"] for p in result["order"]] == list(range(9, -1, -1))


def test_sort_over_budget_returns_input_order():
    batch = service()
    particles = [part(index, float(index)) for index in range(2000)]
    result = batch.sort_particles(
        particles, (0.0, 0.0, 100.0), budget_ms=0.5
    )
    assert result["sorted"] is False
    assert result["budget_exceeded"] is True
    assert result["order"] == particles


def test_transparent_particles_do_not_write_depth():
    batch = service()
    assert batch.depth_write(part(1, 10.0, alpha=0.5)) is False


def test_opaque_particles_write_depth():
    batch = service()
    assert batch.depth_write(part(1, 10.0, alpha=1.0)) is True
