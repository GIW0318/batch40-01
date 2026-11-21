import pytest

import asset_hot_reload_graph as ahr


def test_repeated_watch_events_are_debounced():
    graph = ahr.HotReloadGraph(debounce_ms=200)
    graph.add_asset("a.mat")
    events = [
        {"path": "a.mat", "at_ms": 0},
        {"path": "a.mat", "at_ms": 50},
        {"path": "a.mat", "at_ms": 100},
    ]
    jobs = graph.watch(events, 300)
    assert [job["path"] for job in jobs] == ["a.mat"]


def test_events_after_debounce_window_reload_again():
    graph = ahr.HotReloadGraph(debounce_ms=200)
    graph.add_asset("a.mat")
    events = [{"path": "a.mat", "at_ms": 0}, {"path": "a.mat", "at_ms": 250}]
    jobs = graph.watch(events, 400)
    assert [job["path"] for job in jobs] == ["a.mat", "a.mat"]


def test_debounce_is_tracked_per_path():
    graph = ahr.HotReloadGraph(debounce_ms=200)
    graph.add_asset("a.mat")
    graph.add_asset("b.mat")
    events = [
        {"path": "a.mat", "at_ms": 0},
        {"path": "b.mat", "at_ms": 10},
        {"path": "a.mat", "at_ms": 20},
    ]
    jobs = graph.watch(events, 300)
    assert [job["path"] for job in jobs] == ["a.mat", "b.mat"]


def test_indirect_dependents_are_found():
    graph = ahr.HotReloadGraph()
    graph.add_asset("tex.png")
    graph.add_asset("mat.mat", deps=["tex.png"])
    graph.add_asset("model.fbx", deps=["mat.mat"])
    assert sorted(graph.dependents("tex.png")) == ["mat.mat", "model.fbx"]


def test_reload_order_puts_dependencies_first():
    graph = ahr.HotReloadGraph()
    graph.add_asset("tex.png")
    graph.add_asset("mat.mat", deps=["tex.png"])
    graph.add_asset("model.fbx", deps=["mat.mat"])
    assert graph.reload_order(["tex.png"]) == ["tex.png", "mat.mat", "model.fbx"]


def test_dependency_cycle_terminates_and_visits_once():
    graph = ahr.HotReloadGraph()
    graph.add_asset("a.mat", deps=["b.mat"])
    graph.add_asset("b.mat", deps=["a.mat"])
    order = graph.reload_order(["a.mat"])
    assert len(order) == 2
    assert sorted(order) == ["a.mat", "b.mat"]


def test_diamond_dependency_is_deduplicated():
    graph = ahr.HotReloadGraph()
    graph.add_asset("a.tex")
    graph.add_asset("b.mat", deps=["a.tex"])
    graph.add_asset("c.mat", deps=["a.tex"])
    graph.add_asset("d.fbx", deps=["b.mat", "c.mat"])
    order = graph.reload_order(["a.tex"])
    assert order[0] == "a.tex"
    assert order[-1] == "d.fbx"
    assert len(order) == 4
    assert len(set(order)) == 4


def test_half_written_file_keeps_previous_version():
    graph = ahr.HotReloadGraph()
    graph.add_asset("a.mat")
    graph.swap("a.mat", "v1")
    assert graph.swap("a.mat", "broken", valid=False) is False
    assert graph.version("a.mat") == "v1"
    assert graph.read("a.mat") == "v1"


def test_lease_prevents_unload_until_release():
    graph = ahr.HotReloadGraph()
    graph.add_asset("a.mat")
    graph.swap("a.mat", "v1")
    handle = graph.lease("a.mat")
    graph.remove_asset("a.mat")
    assert graph.read("a.mat") == "v1"
    assert graph.release(handle) is True
    assert graph.read("a.mat") is None


def test_shader_reload_invalidates_variant_cache():
    graph = ahr.HotReloadGraph()
    graph.add_asset("shader.glsl", kind="shader")
    graph.add_asset("mat.mat", deps=["shader.glsl"], kind="material")
    graph.cache_variant("mat.mat", "variant-a")
    assert graph.variants("mat.mat") == ["variant-a"]
    graph.swap("shader.glsl", "v2")
    assert graph.variants("mat.mat") == []


def test_large_asset_reload_is_deferred():
    graph = ahr.HotReloadGraph(large_asset_mb=50.0)
    graph.add_asset("big.fbx", size_mb=120.0)
    graph.add_asset("small.mat", size_mb=2.0)
    assert graph.reload("big.fbx")["deferred"] is True
    assert graph.reload("small.mat")["deferred"] is False


def test_remove_asset_drops_dangling_references():
    graph = ahr.HotReloadGraph()
    graph.add_asset("tex.png")
    graph.add_asset("model.fbx", deps=["tex.png"])
    graph.remove_asset("tex.png")
    assert "tex.png" not in graph.deps("model.fbx")
    assert graph.dependents("tex.png") == []
    assert graph.read("tex.png") is None


def test_poll_detects_changes_when_watcher_misses_events():
    graph = ahr.HotReloadGraph()
    graph.add_asset("net.mat", mtime=1)
    assert graph.poll({"net.mat": 1}) == []
    assert graph.poll({"net.mat": 2}) == ["net.mat"]


def test_unknown_and_empty_paths_are_rejected():
    graph = ahr.HotReloadGraph()
    with pytest.raises(ValueError):
        graph.add_asset("")
    with pytest.raises(ValueError):
        graph.swap("missing.mat", "v1")
    with pytest.raises(ValueError):
        graph.reload("missing.mat")


def test_reload_order_is_independent_of_insertion_order():
    first = ahr.HotReloadGraph()
    first.add_asset("tex.png")
    first.add_asset("mat.mat", deps=["tex.png"])
    first.add_asset("model.fbx", deps=["mat.mat"])
    second = ahr.HotReloadGraph()
    second.add_asset("model.fbx", deps=["mat.mat"])
    second.add_asset("mat.mat", deps=["tex.png"])
    second.add_asset("tex.png")
    assert first.reload_order(["tex.png"]) == second.reload_order(["tex.png"])
