import pytest

import navmesh_tile_bake as ntb


def edge(side, z=1.0, slope=20.0, length=8.0):
    if side == "east":
        start, end = (32.0, 0.0, z), (32.0, length, z)
    elif side == "west":
        start, end = (32.0, 0.0, z), (32.0, length, z)
    elif side == "north":
        start, end = (0.0, 32.0, z), (length, 32.0, z)
    else:
        start, end = (0.0, 0.0, z), (length, 0.0, z)
    return {"side": side, "start": start, "end": end, "slope_deg": slope}


def pair(baker, a_z=1.0, b_z=1.3, a_slope=20.0, b_slope=22.0, length=8.0):
    baker.add_tile(0, 0, [edge("east", a_z, a_slope, length)])
    baker.add_tile(1, 0, [edge("west", b_z, b_slope, length)])
    return baker.stitch((0, 0), (1, 0))


def test_height_within_step_connects_neighbour_tiles():
    baker = ntb.NavMeshTileBaker(step_height=0.4, agent_radius=0.6)
    portal = pair(baker, a_z=1.0, b_z=1.3)
    assert portal["connected"] is True
    assert portal["width"] == pytest.approx(8.0 - 1.2)


def test_height_gap_beyond_step_does_not_connect():
    baker = ntb.NavMeshTileBaker(step_height=0.4, agent_radius=0.6)
    portal = pair(baker, a_z=1.0, b_z=2.0)
    assert portal["connected"] is False
    assert portal["reason"] == "height_gap"


def test_small_slope_difference_still_stitches():
    baker = ntb.NavMeshTileBaker(max_slope=45.0, agent_radius=0.6)
    portal = pair(baker, a_slope=30.0, b_slope=33.0)
    assert portal["connected"] is True


def test_slope_above_limit_does_not_stitch():
    baker = ntb.NavMeshTileBaker(max_slope=45.0, agent_radius=0.6)
    portal = pair(baker, a_slope=30.0, b_slope=50.0)
    assert portal["connected"] is False
    assert portal["reason"] == "slope"


def test_agent_radius_is_applied_across_the_boundary():
    wide = ntb.NavMeshTileBaker(agent_radius=0.6)
    assert pair(wide, length=2.0)["connected"] is True
    narrow = ntb.NavMeshTileBaker(agent_radius=0.6)
    portal = pair(narrow, length=1.0)
    assert portal["connected"] is False
    assert portal["reason"] == "too_narrow"


def test_offmesh_links_are_bidirectional():
    baker = ntb.NavMeshTileBaker()
    baker.add_tile(0, 0, [edge("east")])
    baker.add_tile(1, 0, [edge("west")])
    baker.offmesh_link((0, 0), (1, 0))
    pairs = {(link["from"], link["to"]) for link in baker.links()}
    assert ((0, 0), (1, 0)) in pairs
    assert ((1, 0), (0, 0)) in pairs


def test_one_way_offmesh_link_is_registered_once():
    baker = ntb.NavMeshTileBaker()
    baker.add_tile(0, 0, [edge("east")])
    baker.add_tile(1, 0, [edge("west")])
    baker.offmesh_link((0, 0), (1, 0), bidir=False)
    assert len(baker.links()) == 1
    assert baker.links()[0]["from"] == (0, 0)


def test_obstacle_on_tile_seam_carves_both_tiles():
    baker = ntb.NavMeshTileBaker()
    baker.add_tile(0, 0, [])
    baker.add_tile(1, 0, [])
    baker.apply_obstacle({"id": "rock", "bounds": (30.0, 10.0, 35.0, 14.0)})
    assert len(baker.carved_cells(0, 0)) > 0
    assert len(baker.carved_cells(1, 0)) > 0
    before = len(baker.carved_cells(1, 0))
    baker.apply_obstacle({"id": "small", "bounds": (4.0, 4.0, 8.0, 8.0)})
    assert len(baker.carved_cells(1, 0)) == before


def test_bake_result_is_deterministic():
    baker = ntb.NavMeshTileBaker()
    baker.add_tile(0, 0, [edge("east")])
    baker.add_tile(1, 0, [edge("west")])
    first = baker.bake()
    second = baker.bake()
    assert first["portals"] == second["portals"]
    assert len(first["portals"]) == 1
    portal = first["portals"][0]
    assert portal["a"] == (0, 0)
    assert portal["b"] == (1, 0)


def test_version_change_rebakes_only_dirty_tile():
    baker = ntb.NavMeshTileBaker()
    baker.add_tile(0, 0, [edge("east")], version="v1")
    baker.add_tile(1, 0, [edge("west")], version="v1")
    assert baker.bake()["baked_tiles"] == 2
    baker.set_tile_version(0, 0, "v2")
    assert baker.dirty_tiles() == [(0, 0)]
    result = baker.bake()
    assert result["baked_tiles"] == 1
    assert result["reused"] == 1


def test_unchanged_tiles_are_reused():
    baker = ntb.NavMeshTileBaker()
    baker.add_tile(0, 0, [edge("east")])
    baker.add_tile(1, 0, [edge("west")])
    baker.bake()
    result = baker.bake()
    assert result["baked_tiles"] == 0
    assert result["reused"] == 2
    assert baker.dirty_tiles() == []


def test_query_on_seam_picks_lower_tile_deterministically():
    baker = ntb.NavMeshTileBaker(tile_size=32.0)
    baker.add_tile(0, 0, [])
    baker.add_tile(1, 0, [])
    for _ in range(5):
        result = baker.query({"x": 32.0, "y": 16.0})
        assert result["tile"] == (0, 0)


def test_vertex_index_width_grows_with_size():
    baker = ntb.NavMeshTileBaker()
    assert baker.index_width(65535) == 16
    assert baker.index_width(65536) == 32
    assert baker.index_width(100000) == 32


def test_duplicate_tile_is_rejected():
    baker = ntb.NavMeshTileBaker()
    baker.add_tile(0, 0, [])
    with pytest.raises(ValueError):
        baker.add_tile(0, 0, [])


def test_invalid_bake_configuration_is_rejected():
    with pytest.raises(ValueError):
        ntb.NavMeshTileBaker(agent_radius=0.0)
    with pytest.raises(ValueError):
        ntb.NavMeshTileBaker(step_height=-0.1)
    with pytest.raises(ValueError):
        ntb.NavMeshTileBaker(max_slope=100.0)
