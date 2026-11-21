import pytest

import jitter_buffer_plc as jbp


def buffer_20ms():
    return jbp.JitterBuffer(frame_ms=20, max_delay_ms=200, sample_rate=48000)


def test_sequence_wrap_comparison():
    buffer = buffer_20ms()
    assert buffer.sequence_newer(5, 65530) is True
    assert buffer.sequence_newer(65530, 5) is False
    assert buffer.sequence_newer(100, 99) is True


def test_out_of_order_packets_are_buffered():
    buffer = buffer_20ms()
    buffer.push(1, [1.0] * 960)
    buffer.push(3, [3.0] * 960)
    buffer.push(2, [2.0] * 960)
    assert buffer.pop()["seq"] == 1
    assert buffer.pop()["seq"] == 2
    assert buffer.pop()["seq"] == 3


def test_duplicate_packet_is_ignored():
    buffer = buffer_20ms()
    buffer.push(1, [1.0] * 960)
    assert buffer.push(1, [9.0] * 960) is False
    assert buffer.stats()["duplicates"] == 1
    assert buffer.pop()["samples"][0] == 1.0


def test_late_packet_after_conceal_is_rejected():
    buffer = buffer_20ms()
    buffer.push(1, [1.0] * 960)
    buffer.push(3, [3.0] * 960)
    assert buffer.pop()["seq"] == 1
    missing = buffer.pop()
    assert missing["concealed"] is True
    assert missing["seq"] == 2
    assert buffer.push(2, [2.0] * 960) is False
    assert buffer.stats()["late"] == 1


def test_target_delay_adapts_to_jitter_and_loss():
    buffer = buffer_20ms()
    low = buffer.adapt_delay(0.0, 5.0)
    high = buffer.adapt_delay(0.1, 80.0)
    assert low <= 60
    assert high >= 150
    assert high > low
    assert buffer.frame_ms <= high <= buffer.max_delay_ms


def test_target_delay_is_clamped():
    buffer = buffer_20ms()
    assert buffer.set_target_delay(1000) == 200
    assert buffer.set_target_delay(1) == 20


def test_concealment_matches_frame_length():
    buffer = buffer_20ms()
    frame = buffer.pop()
    assert len(frame["samples"]) == 960
    assert frame["length"] == 960
    assert frame["concealed"] is True


def test_silence_frame_advances_timeline():
    buffer = buffer_20ms()
    buffer.push(1, [0.0] * 960)
    assert buffer.pop()["seq"] == 1
    buffer.push(3, [3.0] * 960)
    assert buffer.pop()["seq"] == 2
    assert buffer.pop()["seq"] == 3


def test_fec_and_real_packet_are_not_double_played():
    buffer = buffer_20ms()
    buffer.push(1, [0.5] * 960, fec=True)
    assert buffer.push(1, [0.9] * 960) is False
    assert buffer.stats()["fec_duplicates"] == 1
    assert buffer.pop()["seq"] == 1
    assert buffer.pop()["seq"] == 2


def test_vad_keeps_soft_speech():
    buffer = buffer_20ms()
    buffer.set_vad_threshold(0.01)
    assert buffer.is_voice_active(0.012) is True
    assert buffer.is_voice_active(0.002) is False


def test_capacity_counts_packets_by_duration():
    buffer = jbp.JitterBuffer(frame_ms=20, max_delay_ms=100, sample_rate=48000)
    assert buffer.capacity_packets() == 5


def test_overflow_is_reported():
    buffer = jbp.JitterBuffer(frame_ms=20, max_delay_ms=100, sample_rate=48000)
    for seq in range(1, 7):
        buffer.push(seq, [float(seq)] * 960)
    assert buffer.stats()["overflow"] == 1
    assert buffer.pop()["seq"] == 2


def test_resample_16k_to_48k():
    buffer = buffer_20ms()
    assert buffer.push(1, [0.1] * 320, sample_rate=16000) is True
    frame = buffer.pop()
    assert len(frame["samples"]) == 960


def test_drift_is_adjusted_without_dropping_frames():
    buffer = buffer_20ms()
    assert buffer.push(1, [0.1] * 961) is True
    frame = buffer.pop()
    assert len(frame["samples"]) == 960
    assert buffer.stats()["drift_frames"] == 1
    assert buffer.stats()["dropped_frames"] == 0


def test_conceal_counter_is_reported():
    buffer = buffer_20ms()
    buffer.pop()
    buffer.pop()
    assert buffer.stats()["concealed"] == 2
