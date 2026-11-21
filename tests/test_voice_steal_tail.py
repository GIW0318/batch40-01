import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import voice_steal_tail as vst


def test_high_priority_voice_survives_steal():
    pool = vst.VoiceStealPool(max_voices=1)
    ambience = pool.acquire("footstep", priority=0, duration=10.0)
    speech = pool.acquire("dialogue", priority=10, duration=10.0)
    assert pool.is_active(speech) is True
    assert pool.is_active(ambience) is False


def test_farther_voice_is_stolen_first():
    pool = vst.VoiceStealPool(max_voices=2)
    near = pool.acquire("near", priority=0, distance=1.0, duration=10.0)
    far = pool.acquire("far", priority=0, distance=10.0, duration=10.0)
    pool.acquire("new", priority=0, distance=0.0, duration=10.0)
    assert pool.is_active(near) is True
    assert pool.is_active(far) is False


def test_shorter_remaining_voice_is_stolen_first():
    pool = vst.VoiceStealPool(max_voices=2)
    long_one = pool.acquire("long", priority=0, duration=10.0)
    short_one = pool.acquire("short", priority=0, duration=1.0)
    pool.acquire("new", priority=0, duration=10.0)
    assert pool.is_active(long_one) is True
    assert pool.is_active(short_one) is False


def test_equal_priority_steal_is_deterministic():
    stolen_ids = []
    for _ in range(20):
        pool = vst.VoiceStealPool(max_voices=2)
        first = pool.acquire("first", priority=0, duration=10.0)
        second = pool.acquire("second", priority=0, duration=10.0)
        pool.acquire("third", priority=0, duration=10.0)
        stolen_ids.append("first" if not pool.is_active(first) else "second")
        assert pool.is_active(second) is True
    assert set(stolen_ids) == {"first"}


def test_stolen_voice_gets_release_envelope_not_hard_stop():
    pool = vst.VoiceStealPool(max_voices=1, release_time=0.05, tail_time=1.0)
    pool.acquire("old", priority=0, duration=10.0)
    pool.acquire("new", priority=10, duration=10.0)
    snap = pool.snapshot()
    assert snap["releasing"] == 1
    assert snap["voices"] == 1
    entry = [e for e in pool.releasing][0]
    assert entry["fade"] > 0
    assert entry["click_free"] is True


def test_release_envelope_keeps_channel_until_envelope_finishes():
    pool = vst.VoiceStealPool(max_voices=1, release_time=0.05)
    pool.acquire("old", priority=0, duration=10.0, channels=2)
    pool.acquire("new", priority=10, duration=10.0, channels=1)
    assert pool.snapshot()["channels"] == 1
    pool.advance(0.051)
    assert pool.snapshot()["releasing"] == 0


def test_reverb_tail_survives_voice_steal():
    pool = vst.VoiceStealPool(max_voices=1, tail_time=1.0)
    pool.acquire("cannon", priority=0, duration=10.0)
    pool.acquire("new", priority=10, duration=10.0)
    assert pool.snapshot()["tails"] == 1
    pool.advance(0.5)
    assert pool.snapshot()["tails"] == 1
    pool.advance(1.0)
    assert pool.snapshot()["tails"] == 0


def test_virtual_voice_resumes_at_original_position():
    pool = vst.VoiceStealPool(max_voices=1)
    handle = pool.acquire("loop", priority=0, duration=2.0, loop=True)
    pool.advance(0.5)
    assert pool.virtualize(handle) is True
    assert pool.snapshot()["virtual"] == 1
    restored = pool.restore(handle)
    assert pool.is_active(restored) is True
    assert pool.position(restored)["elapsed"] == pytest.approx(0.5)


def test_stereo_voice_uses_one_voice_slot():
    pool = vst.VoiceStealPool(max_voices=4)
    pool.acquire("a", channels=2)
    pool.acquire("b", channels=2)
    snap = pool.snapshot()
    assert snap["voices"] == 2
    assert snap["channels"] == 4


def test_channel_count_drops_when_stereo_voice_is_stolen():
    pool = vst.VoiceStealPool(max_voices=2)
    pool.acquire("a", channels=2)
    pool.acquire("b", channels=2)
    pool.acquire("c", channels=1)
    snap = pool.snapshot()
    assert snap["voices"] == 2
    assert snap["channels"] == 3


def test_voice_bus_is_not_stolen_by_sfx_flood():
    pool = vst.VoiceStealPool(max_voices=4, reserved={"voice": 1})
    speech = pool.acquire("speech", priority=0, bus="voice")
    for i in range(3):
        pool.acquire("sfx{}".format(i), priority=0, bus="sfx")
    pool.acquire("sfx3", priority=0, bus="sfx")
    assert pool.is_active(speech) is True
    assert pool.snapshot()["voices"] == 4


def test_loop_fade_parameters_survive_restore():
    pool = vst.VoiceStealPool(max_voices=2)
    handle = pool.acquire("loop", loop=True, duration=2.0, fade_in=0.1, fade_out=0.2)
    pool.advance(0.3)
    pool.virtualize(handle)
    restored = pool.restore(handle)
    assert restored["loop"] is True
    assert restored["fade_in"] == pytest.approx(0.1)
    assert restored["fade_out"] == pytest.approx(0.2)


def test_stale_generation_does_not_kill_new_voice():
    pool = vst.VoiceStealPool(max_voices=1)
    old = pool.acquire("old", priority=0)
    new = pool.acquire("new", priority=10)
    assert pool.release(old) is False
    assert pool.is_active(new) is True
    assert pool.snapshot()["voices"] == 1


def test_concurrent_acquire_never_exceeds_max_voices():
    pool = vst.VoiceStealPool(max_voices=4)
    barrier = threading.Barrier(16)

    def worker(i):
        barrier.wait()
        pool.acquire("s{}".format(i), priority=i, duration=10.0, channels=2)
        return True

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(worker, range(16)))
    assert all(results)
    snap = pool.snapshot()
    assert snap["voices"] == 4
    assert snap["channels"] <= 8


def test_release_is_idempotent_and_rejects_stale_handles():
    pool = vst.VoiceStealPool(max_voices=2)
    handle = pool.acquire("a", duration=1.0)
    assert pool.release(handle) is True
    assert pool.release(handle) is False
    assert pool.release({"id": 999, "generation": 1}) is False
