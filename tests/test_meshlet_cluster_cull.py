import pytest

import meshlet_cluster_cull as mcc


def camera_clusters():
    culler = mcc.MeshletCuller()
    culler.add_cluster("front", center=(0.0, 5.0, 0.0), radius=1.0)
    culler.add_cluster("back", center=(0.0, -5.0, 0.0), radius=1.0)
    return culler


def test_scaled_bounds_cover_scaled_object():
    culler = mcc.MeshletCuller()
    culler.add_cluster("rock", center=(1.0, 0.0, 0.0), radius=1.0)
    center, radius = culler.scaled_bounds("rock", 3.0)
    assert center == (3.0, 0.0, 0.0)
    assert radius == pytest.approx(3.0)


def test_hierarchy_bounds_cover_children():
    culler = mcc.MeshletCuller()
    culler.add_cluster("root", center=(0.0, 0.0, 0.0), radius=0.1)
    culler.add_cluster("left", parent="root", center=(-10.0, 0.0, 0.0), radius=2.0)
    culler.add_cluster("right", parent="root", center=(10.0, 0.0, 0.0), radius=2.0)
    center, radius = culler.hierarchy_bounds("root")
    assert radius >= 12.0


def test_frustum_culls_cluster_behind_camera():
    culler = camera_clusters()
    assert culler.cull() == ["front"]


def test_sphere_intersecting_near_plane_is_kept():
    culler = mcc.MeshletCuller()
    culler.add_cluster("near", center=(0.0, -0.5, 0.0), radius=1.0)
    culler.add_cluster("far", center=(0.0, -2.0, 0.0), radius=1.0)
    assert culler.cull() == ["near"]


def test_solid_cluster_is_backface_culled():
    culler = mcc.MeshletCuller()
    culler.add_cluster("rock", center=(0.0, 5.0, 0.0), radius=1.0,
                       cone_axis=(0.0, -1.0, 0.0), backface_cull=True)
    assert culler.cull() == []


def test_thin_wall_survives_backface():
    culler = mcc.MeshletCuller()
    culler.add_cluster("wall", center=(0.0, 5.0, 0.0), radius=1.0,
                       cone_axis=(0.0, -1.0, 0.0), backface_cull=False)
    assert culler.cull() == ["wall"]


def test_index_width_follows_vertex_count():
    culler = mcc.MeshletCuller()
    culler.add_cluster("small", vertex_count=255)
    culler.add_cluster("medium", vertex_count=256)
    culler.add_cluster("large", vertex_count=70000)
    assert culler.index_width("small") == 8
    assert culler.index_width("medium") == 16
    assert culler.index_width("large") == 32


def test_lod_seam_stitch_returns_pairs():
    culler = mcc.MeshletCuller()
    culler.add_cluster("low", vertex_count=64)
    culler.add_cluster("high", vertex_count=256)
    culler.set_lod("low", 0)
    culler.set_lod("high", 1)
    assert culler.stitch("low", "high") == [("low", "high")]


def test_no_stitch_for_same_lod():
    culler = mcc.MeshletCuller()
    culler.add_cluster("a", vertex_count=64)
    culler.add_cluster("b", vertex_count=256)
    culler.set_lod("a", 0)
    culler.set_lod("b", 0)
    assert culler.stitch("a", "b") == []


def test_build_is_cached_until_dirty():
    culler = camera_clusters()
    culler.cull()
    assert culler.build_count() == 1
    culler.cull()
    assert culler.build_count() == 1
    culler.mark_dirty()
    culler.cull()
    assert culler.build_count() == 2


def test_async_cull_rejects_stale_frame():
    culler = camera_clusters()
    culler.submit_async(1, camera_pos=(0.0, 0.0, 0.0), camera_dir=(0.0, 1.0, 0.0))
    assert culler.apply_async(2) == []
    assert culler.stats()["stale_async"] == 1


def test_async_cull_accepts_matching_frame():
    culler = camera_clusters()
    culler.submit_async(7, camera_pos=(0.0, 0.0, 0.0), camera_dir=(0.0, 1.0, 0.0))
    assert culler.apply_async(7) == ["front"]


def test_instanced_cull_is_per_instance():
    culler = mcc.MeshletCuller()
    culler.add_cluster("body", center=(0.0, 5.0, 0.0), radius=1.0)
    instances = [
        {"id": "guard_a", "offset": (0.0, 0.0, 0.0)},
        {"id": "guard_b", "offset": (0.0, -10.0, 0.0)},
    ]
    assert culler.cull(instances=instances) == [("body", "guard_a")]


def test_oversized_cluster_is_split():
    culler = mcc.MeshletCuller(max_vertices=255)
    culler.add_cluster("heavy", vertex_count=300)
    parts = culler.split_oversized()
    assert len(parts["heavy"]) == 2
    assert sum(culler.vertex_count(part) for part in parts["heavy"]) == 300
    assert all(culler.vertex_count(part) <= 255 for part in parts["heavy"])


def test_shadow_pass_uses_its_own_visibility():
    culler = mcc.MeshletCuller()
    culler.add_cluster("wall", center=(0.0, 5.0, 0.0), radius=1.0)
    main_view = culler.cull(camera_pos=(0.0, 0.0, 0.0), camera_dir=(0.0, 1.0, 0.0))
    shadow_view = culler.cull(camera_pos=(0.0, 0.0, 0.0), camera_dir=(0.0, -1.0, 0.0))
    assert main_view == ["wall"]
    assert shadow_view == []
