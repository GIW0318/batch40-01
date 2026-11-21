import pytest

import texture_residency_queue as trq


def upload(queue, texture_id, mip=0, layer=0, size=(64, 64), compressed=16, resident=256):
    assert queue.request(
        texture_id,
        mip,
        layer=layer,
        size=size,
        compressed_bytes=compressed,
        resident_bytes=resident,
    )
    token = queue.next_io()
    assert token is not None
    assert queue.finish_io(token) is True


def test_requests_ordered_by_visibility_distance():
    queue = trq.TextureResidencyQueue(budget_bytes=100000)
    queue.set_visible("far", 100.0)
    queue.set_visible("mid", 50.0)
    queue.set_visible("near", 10.0)
    queue.request("far", 0)
    queue.request("mid", 0)
    queue.request("near", 0)
    assert queue.pending() == [("near", 0, 0), ("mid", 0, 0), ("far", 0, 0)]


def test_duplicate_mip_request_is_ignored():
    queue = trq.TextureResidencyQueue()
    assert queue.request("rock", 0) is True
    assert queue.request("rock", 0) is False
    assert len(queue.pending()) == 1
    assert queue.stats()["duplicates"] == 1


def test_in_flight_request_not_requeued():
    queue = trq.TextureResidencyQueue()
    assert queue.request("rock", 0) is True
    token = queue.next_io()
    assert token is not None
    assert queue.request("rock", 0) is False
    assert queue.pending() == []


def test_eviction_skips_in_use_texture():
    queue = trq.TextureResidencyQueue(budget_bytes=1000)
    upload(queue, "ground", resident=100)
    upload(queue, "wall", resident=100)
    queue.mark_in_use("ground")
    assert queue.evict(100) == ["wall"]
    assert queue.resident_mips("ground") == [0]
    assert queue.resident_bytes() == 100


def test_eviction_raises_when_only_in_use_remains():
    queue = trq.TextureResidencyQueue(budget_bytes=1000)
    upload(queue, "ground", resident=100)
    queue.mark_in_use("ground")
    with pytest.raises(MemoryError):
        queue.evict(50)


def test_budget_uses_resident_size_not_compressed():
    queue = trq.TextureResidencyQueue(budget_bytes=1000)
    upload(queue, "road", compressed=10, resident=80)
    assert queue.resident_bytes() == 80


def test_residency_budget_overflow_raises():
    queue = trq.TextureResidencyQueue(budget_bytes=100)
    upload(queue, "road", resident=80)
    assert queue.request("big", 0, resident_bytes=30) is True
    token = queue.next_io()
    with pytest.raises(MemoryError):
        queue.finish_io(token)


def test_lower_mip_does_not_overwrite_higher():
    queue = trq.TextureResidencyQueue(budget_bytes=1000)
    upload(queue, "road", mip=0, resident=10)
    assert queue.request("road", 3, resident_bytes=10) is True
    token = queue.next_io()
    assert queue.finish_io(token) is False
    assert queue.resident_mips("road") == [0]


def test_cancelled_io_callback_is_ignored():
    queue = trq.TextureResidencyQueue()
    assert queue.request("road", 0) is True
    token = queue.next_io()
    queue.cancel_io(token)
    assert queue.finish_io(token) is False
    assert queue.resident_mips("road") == []


def test_feedback_keeps_hottest_tiles():
    queue = trq.TextureResidencyQueue(max_feedback=3)
    for tile, weight in [("a", 0.1), ("b", 0.9), ("c", 0.5), ("d", 0.7)]:
        queue.record_feedback(tile, weight)
    assert queue.hottest_tiles() == ["b", "d", "c"]


def test_defragment_skips_sampled_blocks():
    queue = trq.TextureResidencyQueue()
    queue.mark_in_use("ground")
    assert queue.defragment(["ground", "wall", "floor"]) == ["wall", "floor"]


def test_new_array_layer_accepts_high_mip():
    queue = trq.TextureResidencyQueue()
    assert queue.add_layer("rock") == 1
    assert queue.request("rock", 0, layer=1) is True
    assert ("rock", 0, 1) in queue.pending()


def test_resident_mips_are_per_layer():
    queue = trq.TextureResidencyQueue(budget_bytes=1000)
    upload(queue, "road", mip=0, layer=0, resident=10)
    assert queue.add_layer("road") == 1
    upload(queue, "road", mip=1, layer=1, resident=10)
    assert queue.resident_mips("road", 0) == [0]
    assert queue.resident_mips("road", 1) == [1]


def test_oversized_texture_is_rejected():
    queue = trq.TextureResidencyQueue(max_texture_size=4096)
    with pytest.raises(ValueError):
        queue.request("huge", 0, size=(8192, 8192))


def test_main_thread_does_not_wait_on_io():
    queue = trq.TextureResidencyQueue()
    assert queue.request("road", 0) is True
    token = queue.next_io()
    queue.finish_io(token)
    queue.pending()
    queue.stats()
    assert queue.stats()["sync_waits"] == 0
