import pytest

import mux_edit_list as mel


def sample(dts, pts, duration=3000, size=100):
    return {"dts": dts, "pts": pts, "duration": duration, "size": size}


def test_negative_first_pts_writes_edit_list():
    mux = mel.MuxEditList(timescale=90000)
    mux.add_track("video", [sample(0, -3000), sample(3000, 0), sample(6000, 3000)], priming=3000)
    edits = mux.edit_list("video")
    assert edits[0]["media_time"] == 3000
    assert edits[0]["segment_duration"] == 6000


def test_audio_and_video_priming_are_trimmed_consistently():
    mux = mel.MuxEditList(timescale=90000)
    audio = [sample(i * 1024, i * 1024, duration=1024, size=50) for i in range(10)]
    mux.add_track("audio", audio, priming=1024)
    mux.add_track("video", [sample(0, 0), sample(3000, 3000), sample(6000, 6000)], priming=3000)
    assert mux.edit_list("audio")[0]["media_time"] == 1024
    assert mux.edit_list("audio")[0]["segment_duration"] == 9216
    assert mux.edit_list("video")[0]["media_time"] == 3000
    assert mux.edit_list("video")[0]["segment_duration"] == 6000


def test_ctts_version_one_preserves_b_frame_offsets():
    mux = mel.MuxEditList(timescale=90000)
    samples = [
        sample(0, 3000),
        sample(3000, 9000),
        sample(6000, 6000),
        sample(9000, 12000),
    ]
    mux.add_track("video", samples)
    table = mux.ctts("video")
    assert table["version"] == 1
    assert table["offsets"] == [3000, 6000, 0, 3000]
    planned = mux.plan()["tracks"]["video"]["samples"]
    assert [s["dts"] for s in planned] == [0, 3000, 6000, 9000]
    assert planned[1]["pts"] == 9000


def test_ctts_version_zero_when_display_matches_decode():
    mux = mel.MuxEditList()
    mux.add_track("video", [sample(0, 0), sample(3000, 3000)])
    table = mux.ctts("video")
    assert table["version"] == 0
    assert table["offsets"] == [0, 0]


def test_timebase_conversion_is_exact_for_long_durations():
    mux = mel.MuxEditList(timescale=90000)
    assert mux.to_ticks(3600.0) == 324000000
    assert mux.to_ticks(0.1) == 9000


def test_subtitle_track_timescale_is_converted():
    mux = mel.MuxEditList(timescale=90000)
    mux.add_track(
        "sub",
        [sample(0, 0, duration=90000), sample(90000, 90000, duration=1000)],
        timescale=1000,
    )
    planned = mux.plan()["tracks"]["sub"]["samples"]
    assert planned[1]["dts"] == 8100000
    assert planned[0]["duration"] == 8100000


def test_32_bit_timestamp_wrap_is_preserved_as_64_bit():
    mux = mel.MuxEditList(timescale=90000)
    big = 2 ** 32 + 500
    mux.add_track("video", [sample(big, big)])
    planned = mux.plan()["tracks"]["video"]["samples"]
    assert planned[0]["dts"] == big
    assert planned[0]["dts"] > 0


def test_interleave_chunks_respect_limit_and_offsets():
    mux = mel.MuxEditList(timescale=90000, max_chunk=1000)
    mux.add_track("video", [sample(0, 0, size=2500), sample(3000, 3000, size=2500)])
    mux.add_track("audio", [sample(0, 0, duration=1024, size=2500)])
    chunks = mux.interleave()
    assert all(c["size"] <= 1000 for c in chunks)
    assert sum(c["size"] for c in chunks) == 7500
    offsets = [c["offset"] for c in chunks]
    assert offsets == sorted(offsets)
    assert all(offset >= 0 for offset in offsets)


def test_metadata_atom_order_keeps_moov_before_mdat():
    mux = mel.MuxEditList()
    assert mux.metadata_order() == ["ftyp", "moov", "mdat"]


def test_missing_last_sample_duration_is_filled():
    mux = mel.MuxEditList(timescale=90000)
    mux.add_track("video", [sample(0, 0), sample(3000, 3000), sample(6000, 6000, duration=0)])
    planned = mux.plan()["tracks"]["video"]["samples"]
    assert planned[-1]["duration"] == 3000
    assert mux.duration() == 9000


def test_movie_duration_is_max_track_end():
    mux = mel.MuxEditList(timescale=90000)
    mux.add_track("video", [sample(0, 0), sample(3000, 3000)])
    audio = [sample(i * 1024, i * 1024, duration=1024) for i in range(10)]
    mux.add_track("audio", audio)
    assert mux.duration() == 10240
    assert mux.plan()["duration"] == 10240


def test_moov_offsets_are_deterministic_and_non_negative():
    mux = mel.MuxEditList(timescale=90000)
    mux.add_track("video", [sample(0, 0), sample(3000, 3000)])
    mux.add_track("audio", [sample(0, 0, duration=1024), sample(1024, 1024, duration=1024)])
    first = mux.build_moov()
    second = mux.build_moov()
    assert first == second
    offsets = [entry["offset"] for entry in first]
    assert offsets == sorted(offsets)
    assert all(offset >= 0 for offset in offsets)


def test_track_without_samples_is_rejected():
    mux = mel.MuxEditList()
    with pytest.raises(ValueError):
        mux.add_track("empty", [])


def test_non_monotonic_dts_is_rejected():
    mux = mel.MuxEditList()
    with pytest.raises(ValueError):
        mux.add_track("video", [sample(0, 0), sample(6000, 6000), sample(3000, 3000)])


def test_invalid_timescales_are_rejected():
    with pytest.raises(ValueError):
        mel.MuxEditList(timescale=0)
    mux = mel.MuxEditList()
    with pytest.raises(ValueError):
        mux.add_track("video", [sample(0, 0)], timescale=0)
