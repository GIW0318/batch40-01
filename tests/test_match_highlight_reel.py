import hashlib

import pytest

import match_highlight_reel as mhr


def event(event_id=1, round_no=1, server_frame=100, client_time=1,
          round_start=0, round_end=1000, killer="a", victim="b",
          view="player", cover_frame=None, kind="kill"):
    payload = {
        "event_id": event_id,
        "round": round_no,
        "server_frame": server_frame,
        "client_time": client_time,
        "round_start": round_start,
        "round_end": round_end,
        "kind": kind,
        "killer": killer,
        "victim": victim,
        "view": view,
    }
    if cover_frame is not None:
        payload["cover_frame"] = cover_frame
    return payload


def reel():
    return mhr.MatchHighlightReel(fps=30, pre_buffer=2, post_buffer=3)


def test_same_kill_is_collected_once():
    service = reel()
    first = service.collect(1, event())
    second = service.collect(1, event())
    assert first["created"] is True
    assert second["created"] is False
    assert first["clip_id"] == second["clip_id"]
    assert len(service.index()) == 1


def test_different_views_create_separate_clips():
    service = reel()
    first = service.collect(1, event(view="player"))
    second = service.collect(1, event(view="spectator"))
    assert first["clip_id"] != second["clip_id"]
    assert second["created"] is True
    assert len(service.index()) == 2


def test_start_uses_server_frame_and_buffer():
    service = reel()
    result = service.collect(1, event(server_frame=100, client_time=1))
    assert result["start"] == 98
    assert result["end"] == 103


def test_end_is_clamped_to_round_end():
    service = reel()
    result = service.collect(1, event(server_frame=100, round_end=101))
    assert result["end"] == 101
    assert result["start"] == 98


def test_start_is_clamped_to_round_start():
    service = reel()
    result = service.collect(1, event(server_frame=11, round_start=10))
    assert result["start"] == 10


def test_audio_bounds_follow_clip_bounds():
    service = reel()
    service.collect(1, event(server_frame=100))
    record = service.index()[0]
    assert record["audio_start"] == record["start"] == 98
    assert record["audio_end"] == record["end"] == 103


def test_index_is_sorted_by_match_time():
    service = reel()
    service.collect(1, event(event_id=1, server_frame=100), match_time=200)
    service.collect(2, event(event_id=2, server_frame=50), match_time=100)
    assert [row["match_id"] for row in service.index()] == [2, 1]
    assert len(service.index(1)) == 1


def test_cover_defaults_to_server_frame_with_timestamp():
    service = reel()
    service.collect(1, event(server_frame=100))
    record = service.index()[0]
    assert record["cover_frame"] == 100
    assert record["cover_ms"] == 3333


def test_cover_outside_clip_is_rejected():
    service = reel()
    with pytest.raises(ValueError):
        service.collect(
            1,
            event(server_frame=100, round_start=90, round_end=110,
                  cover_frame=80),
        )


def test_publish_before_cut_is_rejected():
    service = reel()
    result = service.collect(1, event())
    with pytest.raises(ValueError):
        service.publish(result["clip_id"])


def test_cut_records_range_and_output_hash():
    service = reel()
    result = service.collect(1, event(server_frame=100))
    cut = service.cut(result["clip_id"])
    expected = hashlib.sha256(
        "{}:98:103".format(result["clip_id"]).encode("utf-8")
    ).hexdigest()
    assert cut["input_start"] == 98
    assert cut["input_end"] == 103
    assert cut["output_hash"] == expected
    assert service.cut(result["clip_id"]) == cut


def test_share_token_validates_and_tamper_is_rejected():
    service = reel()
    result = service.collect(1, event())
    service.cut(result["clip_id"])
    published = service.publish(result["clip_id"], now=1000, ttl=60)
    assert published["expires_at"] == 1060
    played = service.playback(
        result["clip_id"], token=published["share_token"], now=1000
    )
    assert played["clip_id"] == result["clip_id"]
    token = published["share_token"]
    bad = token[:-1] + ("0" if token[-1] != "0" else "1")
    with pytest.raises(PermissionError):
        service.playback(result["clip_id"], token=bad, now=1000)
    with pytest.raises(PermissionError):
        service.playback(result["clip_id"], token=None, now=1000)


def test_republish_returns_same_token():
    service = reel()
    result = service.collect(1, event())
    service.cut(result["clip_id"])
    first = service.publish(result["clip_id"], now=10)
    second = service.publish(result["clip_id"], now=20)
    assert second == first


def test_expire_marks_record_and_blocks_playback():
    service = reel()
    result = service.collect(1, event())
    service.cut(result["clip_id"])
    published = service.publish(result["clip_id"], now=0, ttl=10)
    assert service.expire(now=10) == 1
    record = service.index()[0]
    assert record["clip_id"] == result["clip_id"]
    assert record["status"] == "expired"
    with pytest.raises(PermissionError):
        service.playback(
            result["clip_id"], token=published["share_token"], now=10
        )


def test_expire_keeps_unpublished_clips():
    service = reel()
    service.collect(1, event(event_id=1))
    service.collect(1, event(event_id=2, server_frame=200))
    assert service.expire(now=10 ** 9) == 0
    assert len(service.index()) == 2
