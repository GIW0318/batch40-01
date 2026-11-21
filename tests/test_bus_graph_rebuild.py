import pytest

import bus_graph_rebuild as bgr


def simple_graph():
    graph = bgr.AudioBusGraph()
    graph.add_bus("sfx", parent="master")
    graph.add_bus("music", parent="master")
    return graph


def test_cycle_connect_rejected():
    graph = bgr.AudioBusGraph()
    graph.add_bus("a", parent="master")
    graph.add_bus("b", parent="a")
    with pytest.raises(ValueError):
        graph.connect("a", "b")


def test_orphan_bus_reported():
    graph = bgr.AudioBusGraph()
    graph.add_bus("fx")
    assert ("orphan", "fx") in graph.validate()


def test_depth_limit_raises():
    graph = bgr.AudioBusGraph(max_depth=2)
    graph.add_bus("a", parent="master")
    graph.add_bus("b", parent="a")
    with pytest.raises(ValueError):
        graph.add_bus("c", parent="b")


def test_invalid_rebuild_keeps_old_graph():
    graph = simple_graph()
    version = graph.graph_version()
    with pytest.raises(ValueError):
        graph.rebuild([("master", None), ("a", "b"), ("b", "a")])
    assert graph.graph_version() == version
    assert graph.validate() == []


def test_gain_ramp_is_linear():
    graph = bgr.AudioBusGraph()
    graph.add_bus("fx", parent="master", gain=0.0)
    graph.set_gain("fx", 1.0, ramp_ms=100)
    assert graph.gain_at("fx") == pytest.approx(0.0)
    graph.tick(50)
    assert graph.gain_at("fx") == pytest.approx(0.5)
    graph.tick(50)
    assert graph.gain_at("fx") == pytest.approx(1.0)


def test_gain_ramp_clamped_at_target():
    graph = bgr.AudioBusGraph()
    graph.add_bus("fx", parent="master", gain=0.0)
    graph.set_gain("fx", 0.8, ramp_ms=10)
    graph.tick(100)
    assert graph.gain_at("fx") == pytest.approx(0.8)
    graph.tick(100)
    assert graph.gain_at("fx") == pytest.approx(0.8)


def test_deferred_destroy_waits_for_voice():
    graph = simple_graph()
    graph.add_voice("v1", "sfx")
    result = graph.rebuild([("master", None), ("music", "master")])
    assert result["applied"] is False
    assert result["reason"] == "voices"
    assert graph.active_bus("sfx") is True


def test_rebuild_applies_after_voice_release():
    graph = simple_graph()
    graph.add_voice("v1", "sfx")
    graph.rebuild([("master", None), ("music", "master")])
    graph.remove_voice("v1")
    result = graph.rebuild([("master", None), ("music", "master")])
    assert result["applied"] is True
    assert graph.active_bus("sfx") is False


def test_send_flags_survive_rebuild():
    graph = simple_graph()
    graph.set_send("sfx", "master", amount=0.25, pre_fader=False)
    graph.rebuild([("master", None), ("sfx", "master"), ("music", "master")])
    config = graph.send_config("sfx", "master")
    assert config["amount"] == pytest.approx(0.25)
    assert config["pre_fader"] is False


def test_snapshot_to_missing_bus_rejected():
    graph = simple_graph()
    snap = graph.snapshot("sfx", {"ghost": {"amount": 1.0, "pre_fader": True}})
    with pytest.raises(ValueError):
        graph.apply_snapshot(snap)


def test_channel_layout_rebuilds_buffers():
    graph = bgr.AudioBusGraph(channels=2)
    assert graph.set_channel_layout("5.1") == 6
    assert graph.channel_count() == 6
    assert graph.set_channel_layout("stereo") == 2
    assert graph.channel_count() == 2


def test_sample_rate_change_recomputes_latency():
    graph = bgr.AudioBusGraph(sample_rate=48000)
    assert graph.latency_samples() == 480
    graph.set_sample_rate(44100)
    assert graph.latency_samples() == 441


def test_budgeted_rebuild_splits_work():
    graph = bgr.AudioBusGraph()
    routes = [("master", None), ("a", "master"), ("b", "a"), ("c", "b")]
    first = graph.rebuild(routes, max_steps=2)
    assert first["applied"] is False
    assert first["pending"] == 2
    second = graph.rebuild(max_steps=2)
    assert second["applied"] is True
    assert graph.active_bus("c") is True


def test_rebuild_is_not_on_main_thread():
    graph = bgr.AudioBusGraph()
    routes = [("master", None), ("a", "master"), ("b", "a"), ("c", "b")]
    graph.rebuild(routes, max_steps=2)
    graph.rebuild(max_steps=2)
    assert graph.stats()["main_thread_ms"] == 0.0


def test_rebuild_increments_version():
    graph = simple_graph()
    version = graph.graph_version()
    graph.rebuild([("master", None), ("sfx", "master"), ("music", "master")])
    assert graph.graph_version() == version + 1
