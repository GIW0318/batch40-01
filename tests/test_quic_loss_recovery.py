import pytest

import quic_loss_recovery as qlr


def tracker(**kwargs):
    guard = qlr.QuicLossTracker(**kwargs)
    guard.on_address_validated()
    return guard


def test_ack_only_packet_is_not_counted_in_flight():
    guard = tracker()
    guard.on_packet_sent(1, 0.0, 50, ack_only=True)
    snapshot = guard.snapshot()
    assert snapshot["in_flight"] == 0
    assert snapshot["sent"] == 1


def test_data_packets_accumulate_in_flight_bytes():
    guard = tracker()
    guard.on_packet_sent(1, 0.0, 100)
    guard.on_packet_sent(2, 0.0, 200)
    assert guard.snapshot()["in_flight"] == 300


def test_duplicate_ack_is_ignored():
    guard = tracker()
    guard.on_packet_sent(1, 0.0, 100)
    assert guard.on_ack(1, 1.0) is True
    assert guard.on_ack(1, 2.0) is False
    snapshot = guard.snapshot()
    assert snapshot["acked"] == 1
    assert snapshot["in_flight"] == 0


def test_packet_threshold_declares_loss():
    guard = tracker()
    for number in range(1, 6):
        guard.on_packet_sent(number, 0.0, 100)
    guard.on_ack(5, 1.0)
    assert guard.detect_lost(1.0) == [1, 2]
    assert guard.detect_lost(2.0) == []


def test_reordered_packet_within_packet_threshold_survives():
    guard = tracker()
    for number in (1, 2, 3):
        guard.on_packet_sent(number, 0.0, 100)
    guard.on_ack(2, 1.0)
    assert guard.detect_lost(1.0) == []
    assert guard.snapshot()["in_flight"] == 200


def test_time_threshold_follows_rtt_not_fixed_200ms():
    guard = tracker()
    guard.on_rtt_sample(200)
    guard.on_packet_sent(1, 0.0, 100)
    guard.on_packet_sent(2, 0.0, 100)
    guard.on_ack(2, 0.5)
    assert guard.detect_lost(210.0) == []
    assert guard.detect_lost(260.0) == [1]


def test_pto_backoff_doubles_per_timeout():
    guard = tracker()
    guard.on_rtt_sample(100)
    assert guard.pto_interval() == pytest.approx(300.0)
    guard.on_pto_timeout(1.0)
    assert guard.pto_interval() == pytest.approx(600.0)
    guard.on_pto_timeout(2.0)
    assert guard.pto_interval() == pytest.approx(1200.0)


def test_ack_resets_pto_backoff():
    guard = tracker()
    guard.on_rtt_sample(100)
    guard.on_pto_timeout(1.0)
    guard.on_pto_timeout(2.0)
    assert guard.pto_interval() == pytest.approx(1200.0)
    guard.on_packet_sent(1, 2.0, 100)
    guard.on_ack(1, 3.0)
    assert guard.pto_interval() == pytest.approx(300.0)
    assert guard.snapshot()["pto_count"] == 0


def test_unvalidated_address_cannot_exceed_three_times_received():
    guard = qlr.QuicLossTracker()
    guard.on_bytes_received(1000)
    assert guard.send_allowance() == 3000
    assert guard.can_send(3000) is True
    assert guard.can_send(3001) is False
    with pytest.raises(ValueError):
        guard.on_packet_sent(1, 0.0, 3001)
    guard.on_packet_sent(1, 0.0, 3000)
    assert guard.send_allowance() == 0


def test_address_validation_removes_amplification_limit():
    guard = qlr.QuicLossTracker()
    guard.on_bytes_received(10)
    with pytest.raises(ValueError):
        guard.on_packet_sent(1, 0.0, 1000)
    guard.on_address_validated()
    assert guard.send_allowance() is None
    assert guard.can_send(1000000) is True
    guard.on_packet_sent(2, 0.0, 1000000)
    assert guard.snapshot()["in_flight"] == 1000000


def test_migration_starts_with_fresh_path_window_and_keeps_old():
    guard = tracker()
    guard.on_packet_sent(1, 0.0, 2000, path_id=0)
    guard.on_ack(1, 1.0)
    assert guard.path_cwnd(0) == 14000
    guard.on_path_migration(1, 2.0)
    assert guard.path_cwnd(1) == 12000
    assert guard.path_cwnd(0) == 14000
    assert guard.snapshot()["current_path"] == 1


def test_stream_retransmission_keeps_offset_data_and_fin():
    guard = tracker()
    guard.write_stream_frame("stream-1", 100, b"payload", fin=True)
    frame = guard.on_stream_lost("stream-1")
    assert frame["stream_id"] == "stream-1"
    assert frame["offset"] == 100
    assert frame["data"] == b"payload"
    assert frame["fin"] is True
    assert guard.snapshot()["stream_retransmits"] == 1


def test_pacer_limits_sending_to_token_bucket():
    guard = tracker()
    guard.configure_pacer(rate=1000, burst=1000)
    assert guard.allow_send(0.0, 1000) is True
    assert guard.allow_send(0.0, 1) is False
    assert guard.allow_send(1.0, 1000) is True


def test_key_update_does_not_drop_1rtt_packets():
    guard = tracker()
    guard.on_key_update()
    guard.on_packet_sent(1, 0.0, 100, packet_type="1rtt")
    snapshot = guard.snapshot()
    assert snapshot["sent"] == 1
    assert snapshot["in_flight"] == 100


def test_snapshot_returns_isolated_copy():
    guard = tracker()
    guard.on_packet_sent(1, 0.0, 100)
    snapshot = guard.snapshot()
    snapshot["paths"][0]["cwnd"] = 999
    snapshot["in_flight"] = 0
    fresh = guard.snapshot()
    assert fresh["paths"][0]["cwnd"] == 12000
    assert fresh["in_flight"] == 100
