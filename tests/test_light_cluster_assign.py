import pytest

import light_cluster_assign as lca


def point(light_id, z, radius=1.0):
    return {
        "id": light_id,
        "pos": (0.0, 0.0, z),
        "radius": radius,
        "type": "point",
    }


def spot(light_id, z, radius=100.0, cos_outer=0.5):
    return {
        "id": light_id,
        "pos": (0.0, 0.0, z),
        "radius": radius,
        "type": "spot",
        "direction": (0.0, 0.0, -1.0),
        "cos_outer": cos_outer,
    }


def service():
    return lca.LightClusterAssign(clusters=8, near=0.1, far=100.0)


def test_slice_gaps_grow_exponentially():
    bounds = service().slice_bounds()
    assert bounds[0] == pytest.approx(0.1)
    assert bounds[-1] == pytest.approx(100.0)
    gaps = [bounds[index + 1] - bounds[index]
            for index in range(len(bounds) - 1)]
    assert all(gaps[index] > gaps[index - 1]
               for index in range(1, len(gaps)))


def test_tiny_near_plane_keeps_valid_slices():
    bounds = service().slice_bounds(near=0.01, far=1000.0)
    assert bounds[0] == pytest.approx(0.01)
    assert bounds[-1] == pytest.approx(1000.0)
    assert len(bounds) == 9
    assert all(bounds[index + 1] > bounds[index]
               for index in range(len(bounds) - 1))


def test_large_light_covers_all_intersecting_clusters():
    cluster = service()
    indices = cluster.assign_light(point(1, 50.0, radius=100.0))
    assert indices == list(range(8))


def test_affects_uses_sphere_distance():
    cluster = service()
    assert cluster.affects(point(1, 10.0, radius=2.0), (1.0, 0.0, 10.0)) is True
    assert cluster.affects(point(1, 10.0, radius=2.0), (3.0, 0.0, 10.0)) is False


def test_cluster_lists_have_no_duplicate_lights():
    cluster = service()
    result = cluster.assign_all([point(1, 1.0, 5.0), point(2, 1.0, 5.0)])
    for ids in result["clusters"].values():
        assert len(ids) == len(set(ids))
    assert sum(len(ids) for ids in result["clusters"].values()) >= 2


def test_spot_cone_excludes_side_points():
    cluster = service()
    assert cluster.affects(spot(1, 10.0), (10.0, 0.0, 9.0)) is False


def test_spot_cone_excludes_points_behind():
    cluster = service()
    assert cluster.affects(spot(1, 10.0), (0.0, 0.0, 20.0)) is False


def test_spot_cone_includes_inside_points():
    cluster = service()
    assert cluster.affects(spot(1, 10.0), (0.0, 0.0, 5.0)) is True


def test_shadow_slot_keeps_ids_above_16_bits():
    cluster = service()
    assert cluster.shadow_slot(70000) == 70000
    assert cluster.shadow_slot(1) == 1


def test_build_reports_the_same_layout_as_slice_bounds():
    cluster = service()
    built = cluster.build(near=0.5, far=50.0)
    assert built["boundaries"] == cluster.slice_bounds(near=0.5, far=50.0)
    assert cluster.view_bounds("main")["boundaries"] == built["boundaries"]
    assert built["near"] == 0.5
    assert built["far"] == 50.0


def test_async_result_from_stale_frame_is_rejected():
    cluster = service()
    result = cluster.submit_async("main", 10, [1, 2])
    assert cluster.accept_async(result, 11) is False


def test_async_result_from_current_frame_is_accepted():
    cluster = service()
    result = cluster.submit_async("main", 10, [1, 2])
    assert cluster.accept_async(result, 10) is True


def test_build_reduces_clusters_to_meet_budget():
    built = service().build(budget_ms=0.05)
    assert built["clusters"] == 2
    assert built["cost_ms"] == pytest.approx(0.04)
    assert built["within_budget"] is True
    assert len(built["boundaries"]) == 3


def test_light_limit_records_overflow():
    cluster = service()
    lights = [point(1, 1.0), point(2, 1.0), point(3, 1.0)]
    result = cluster.assign_all(lights, limit=2)
    assert result["unassigned"] == [3]
    assert cluster.overflow() == [3]


def test_main_and_reflection_keep_separate_layouts():
    cluster = service()
    cluster.build(near=0.1, far=100.0, view="main")
    cluster.build(near=1.0, far=10.0, view="reflection")
    assert cluster.view_bounds("main")["near"] == 0.1
    assert cluster.view_bounds("reflection")["near"] == 1.0
    assert (cluster.view_bounds("main")["boundaries"]
            != cluster.view_bounds("reflection")["boundaries"])
