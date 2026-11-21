import pytest

import lightmap_atlas_pack as lap


def sample_islands():
    return [
        {"id": "wall", "size": (100.0, 60.0)},
        {"id": "floor", "size": (200.0, 80.0)},
        {"id": "table", "size": (40.0, 40.0)},
    ]


def test_padding_scales_with_resolution():
    packer = lap.AtlasPacker(resolution=2048, padding=4)
    assert packer.padding_for(2048) == 4
    assert packer.padding_for(4096) == 8


def test_packed_islands_do_not_overlap():
    packer = lap.AtlasPacker(resolution=1024, padding=4)
    packer.pack(sample_islands())
    assert packer.overlap_pairs() == []


def test_rotated_island_bounds_cover_rotated_size():
    packer = lap.AtlasPacker(resolution=1024)
    packer.pack([{"id": "sign", "size": (100.0, 20.0), "rotation": 90.0}])
    x, y, width, height = packer.rects()["sign"]
    assert width == pytest.approx(20.0, abs=0.001)
    assert height == pytest.approx(100.0, abs=0.001)


def test_npot_resolution_rejected():
    with pytest.raises(ValueError):
        lap.AtlasPacker(resolution=3000)


def test_texel_center_alignment():
    packer = lap.AtlasPacker(resolution=2048)
    u, v = packer.snap_uv((0.5013, 0.2501))
    assert u == pytest.approx((1026 + 0.5) / 2048)
    assert v == pytest.approx((512 + 0.5) / 2048)


def test_resize_remaps_references():
    packer = lap.AtlasPacker(resolution=2048)
    packer.pack([{"id": "wall", "size": (100.0, 100.0)}])
    before = packer.uv_for("wall", 0.5, 0.5)
    packer.resize(4096)
    after = packer.uv_for("wall", 0.5, 0.5)
    assert after == pytest.approx(before)


def test_deterministic_pack_order():
    islands = sample_islands()
    first = lap.AtlasPacker(resolution=1024)
    first.pack(islands)
    second = lap.AtlasPacker(resolution=1024)
    second.pack(list(reversed(islands)))
    assert first.rects() == second.rects()


def test_oversized_resolution_clamped_with_warning():
    packer = lap.AtlasPacker(resolution=16384, max_resolution=8192)
    assert packer.resolution == 8192
    assert len(packer.warnings()) >= 1


def test_tiny_island_kept():
    packer = lap.AtlasPacker(resolution=256)
    packer.pack([{"id": "cup", "size": (1.0, 1.0)}])
    assert "cup" in packer.rects()


def test_transparent_island_skipped():
    packer = lap.AtlasPacker(resolution=256)
    packer.pack([
        {"id": "glass", "size": (50.0, 50.0), "transparent": True},
        {"id": "wall", "size": (50.0, 50.0)},
    ])
    assert "glass" not in packer.rects()
    assert "wall" in packer.rects()


def test_texel_density_scales_with_world_size():
    packer = lap.AtlasPacker(resolution=2048)
    assert packer.texels_for(1.0, 16) == 16
    assert packer.texels_for(20.0, 16) == 320


def test_padding_gap_between_neighbors():
    packer = lap.AtlasPacker(resolution=1024, padding=4)
    packer.pack([{"id": "a", "size": (100.0, 100.0)}, {"id": "b", "size": (100.0, 100.0)}])
    ax, ay, aw, ah = packer.rects()["a"]
    bx, by, bw, bh = packer.rects()["b"]
    assert bx - (ax + aw) == pytest.approx(4.0)


def test_atlas_overflow_raises():
    packer = lap.AtlasPacker(resolution=1024)
    with pytest.raises(lap.AtlasFullError):
        packer.pack([{"id": "a", "size": (800.0, 800.0)}, {"id": "b", "size": (800.0, 800.0)}])


def test_bake_upload_dirty_once():
    packer = lap.AtlasPacker(resolution=256)
    assert packer.consume_upload() is False
    packer.commit_bake()
    assert packer.consume_upload() is True
    assert packer.consume_upload() is False


def test_validate_reports_no_problems():
    packer = lap.AtlasPacker(resolution=1024)
    packer.pack(sample_islands())
    assert packer.validate() == []
