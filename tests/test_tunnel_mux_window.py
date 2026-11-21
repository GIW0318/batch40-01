import pytest

import tunnel_mux_window as tmw


def tunnel(**kwargs):
    return tmw.TunnelMuxWindow(**kwargs)


def test_stream_window_blocks_and_consume_restores():
    link = tunnel(connection_window=65536, stream_window=100)
    link.open_stream("s1")
    link.send("s1", 100)
    with pytest.raises(ValueError):
        link.send("s1", 1)
    update = link.consume("s1", 100)
    assert update["window_update"] == 100
    assert link.send("s1", 100)["stream_remaining"] == 0


def test_connection_window_is_shared_across_streams():
    link = tunnel(connection_window=100, stream_window=100)
    link.open_stream("s1")
    link.open_stream("s2")
    link.send("s1", 60)
    link.send("s2", 40)
    with pytest.raises(ValueError):
        link.send("s2", 1)
    link.consume("s1", 60)
    assert link.send("s2", 60)["connection_remaining"] == 0


def test_stream_window_cannot_exceed_connection_window():
    with pytest.raises(ValueError):
        tunnel(connection_window=100, stream_window=200)


def test_half_close_local_still_receives():
    link = tunnel()
    link.open_stream("s1")
    assert link.close_stream("s1", "local") == "half_closed_local"
    with pytest.raises(ValueError):
        link.send("s1", 1)
    assert link.on_data("s1", 0, b"hi") == "delivered"
    assert link.close_stream("s1", "remote") == "closed"
    with pytest.raises(ValueError):
        link.on_data("s1", 2, b"!")


def test_reset_rejects_later_data():
    link = tunnel()
    link.open_stream("s1")
    link.reset_stream("s1")
    assert link.state("s1") == "closed"
    with pytest.raises(ValueError):
        link.on_data("s1", 0, b"late")


def test_duplicate_open_is_rejected():
    link = tunnel()
    link.open_stream("s1")
    with pytest.raises(ValueError):
        link.open_stream("s1")


def test_closed_stream_id_cannot_be_reopened():
    link = tunnel()
    link.open_stream("s1")
    link.close_stream("s1", "local")
    link.close_stream("s1", "remote")
    with pytest.raises(ValueError):
        link.open_stream("s1")
    with pytest.raises(ValueError):
        link.on_data("s1", 0, b"late")


def test_out_of_order_frames_are_buffered():
    link = tunnel()
    link.open_stream("s1")
    assert link.on_data("s1", 1, b"b") == "buffered"
    assert link.deliverable("s1") == []
    assert link.on_data("s1", 0, b"a") == "delivered"
    assert link.deliverable("s1") == [b"a", b"b"]


def test_frame_length_over_limit_rejected_before_buffering():
    link = tunnel(max_frame=16)
    link.open_stream("s1")
    with pytest.raises(ValueError):
        link.on_data("s1", 0, b"x", size=17)
    assert link.deliverable("s1") == []
    assert link.on_data("s1", 0, b"x") == "delivered"
    assert link.deliverable("s1") == [b"x"]


def test_keepalive_uses_progress_not_silence():
    link = tunnel(idle_timeout=30.0)
    link.open_stream("s1", now=0.0)
    assert link.keepalive(20.0) == []
    link.send("s1", 1, now=25.0)
    assert link.keepalive(40.0) == []
    assert link.keepalive(56.0) == ["s1"]


def test_reconnect_resumes_from_acked_offset():
    link = tunnel()
    link.open_stream("s1")
    link.send("s1", 1000)
    link.ack("s1", 600)
    assert link.resume_offset("s1") == 600
    with pytest.raises(ValueError):
        link.ack("s1", 1001)
    link.ack("s1", 1000)
    assert link.resume_offset("s1") == 1000


def test_control_frames_schedule_before_data():
    link = tunnel()
    link.open_stream("s1")
    link.pending("s1", 100)
    link.queue_control(8)
    assert link.schedule()["kind"] == "control"
    assert link.schedule()["stream"] == "s1"


def test_weighted_scheduling_is_fair():
    link = tunnel()
    link.open_stream("s1", weight=2)
    link.open_stream("s2", weight=1)
    link.pending("s1", 100)
    link.pending("s2", 100)
    counts = {"s1": 0, "s2": 0}
    for _ in range(30):
        chosen = link.schedule()["stream"]
        counts[chosen] += 1
    assert counts == {"s1": 20, "s2": 10}


def test_snapshot_is_an_isolated_copy():
    link = tunnel()
    link.open_stream("s1")
    link.send("s1", 100)
    snapshot = link.snapshot()
    assert snapshot["connection_window"] == 65436
    assert snapshot["streams"]["s1"]["window"] == 16284
    snapshot["connection_window"] = 0
    snapshot["streams"]["s1"]["window"] = 0
    fresh = link.snapshot()
    assert fresh["connection_window"] == 65436
    assert fresh["streams"]["s1"]["window"] == 16284


def test_in_order_data_is_delivered_immediately():
    link = tunnel()
    link.open_stream("s1")
    assert link.on_data("s1", 0, b"hello") == "delivered"
    assert link.on_data("s1", 5, b"world") == "delivered"
    assert link.deliverable("s1") == [b"hello", b"world"]
