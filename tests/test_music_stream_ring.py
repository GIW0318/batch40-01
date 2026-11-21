import pytest

import music_stream_ring as msr


def ring(capacity=8, guard=2):
    return msr.MusicStreamRing(capacity_frames=capacity, guard_frames=guard)


def test_write_respects_guard_distance():
    buffer = ring()
    assert buffer.write([1, 2, 3, 4, 5, 6, 7, 8]) == 6
    assert buffer.free() == 0


def test_read_across_wrap_preserves_order():
    buffer = ring()
    assert buffer.write([1, 2, 3, 4, 5, 6]) == 6
    assert buffer.read(3) == [1, 2, 3]
    assert buffer.write([7, 8, 9, 10]) == 3
    assert buffer.read(6) == [4, 5, 6, 7, 8, 9]


def test_seek_invalidates_inflight_decode():
    buffer = ring(capacity=48000)
    token = buffer.begin_decode("battle")
    buffer.seek(1000)
    assert buffer.decode_write(token, [1, 2, 3]) == 0
    assert buffer.stats()["stale_decodes"] == 1


def test_decode_works_after_seek():
    buffer = ring(capacity=48000)
    buffer.begin_decode("battle")
    buffer.seek(10)
    token = buffer.begin_decode("battle")
    assert buffer.decode_write(token, [1, 2, 3]) == 3
    assert buffer.read(3) == [1, 2, 3]


def test_priming_samples_trimmed():
    buffer = ring(capacity=48000)
    assert buffer.trim_priming(list(range(10)), 3) == [3, 4, 5, 6, 7, 8, 9]


def test_underrun_fades_instead_of_zero_fill():
    buffer = ring(capacity=48000, guard=64)
    buffer.write([5.0, 6.0])
    assert buffer.read(2) == [5.0, 6.0]
    faded = buffer.underrun(3)
    assert faded == pytest.approx([3.0, 1.5, 0.75])
    assert faded[0] > faded[1] > faded[2] > 0.0


def test_crossfade_resamples_different_rates():
    buffer = ring(capacity=48000)
    first = [0.0] * 1000
    second = [1.0] * 500
    out = buffer.crossfade(first, 48000, second, 24000, 100)
    assert len(out) == 1000
    assert buffer.stats()["resampled"] == 1


def test_prefetch_priority_orders_tracks():
    buffer = ring(capacity=48000)
    buffer.enqueue("sfx", 1)
    buffer.enqueue("music", 5)
    buffer.enqueue("ambient", 3)
    assert buffer.next_prefetch() == "music"
    assert buffer.next_prefetch() == "ambient"
    assert buffer.next_prefetch() == "sfx"


def test_device_rate_change_resizes_capacity():
    buffer = msr.MusicStreamRing(capacity_frames=48000, device_rate=48000)
    assert buffer.capacity_for(44100) == 44100
    buffer.set_device_rate(44100)
    assert buffer.capacity() == 44100


def test_release_defers_until_decode_callback():
    buffer = ring(capacity=48000)
    buffer.begin_decode("battle")
    assert buffer.release_track("battle") is False
    assert buffer.stats()["released"] == 0


def test_release_completes_after_callback():
    buffer = ring(capacity=48000)
    token = buffer.begin_decode("battle")
    buffer.release_track("battle")
    assert buffer.end_decode(token) is True
    assert buffer.stats()["released"] == 1


def test_memory_pool_sized_per_track():
    buffer = ring(capacity=48000)
    pool = buffer.allocate_pool({"short": 100, "battle": 200, "ambient": 50})
    assert pool == {"short": 100, "battle": 200, "ambient": 50}


def test_underrun_counter_reported():
    buffer = ring(capacity=48000)
    buffer.underrun(2)
    buffer.underrun(2)
    assert buffer.stats()["underruns"] == 2


def test_total_written_uses_wide_counter():
    buffer = ring(capacity=48000)
    assert buffer.write([1, 2, 3, 4]) == 4
    assert buffer.total_written() == 4
    assert buffer.stats()["counter_bits"] == 64


def test_crossfade_output_length_matches_first_stream():
    buffer = ring(capacity=48000)
    first = [0.0] * 1000
    second = [1.0] * 500
    out = buffer.crossfade(first, 48000, second, 24000, 100)
    assert len(out) == 1000
    assert out[500:] == first[500:]
