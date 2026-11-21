import pytest

import segment_encode_plan as sep

TB = 90000


def uniform_frames(count, duration=3000):
    frames = []
    t = 0
    for _ in range(count):
        frames.append({"dts": t, "pts": t, "duration": duration})
        t += duration
    return frames


def test_first_frame_of_each_segment_is_independent():
    planner = sep.SegmentEncodePlan(timebase=TB, segment_duration=1.0)
    segments = planner.plan(uniform_frames(90))
    assert len(segments) == 3
    assert [s["keyframes"] for s in segments] == [[0], [30], [60]]
    assert all(s["independent"] is True for s in segments)
    assert all(s["depends_on_previous"] is False for s in segments)


def test_scene_cut_on_boundary_does_not_add_second_keyframe():
    frames = uniform_frames(90)
    frames[30]["scene_cut"] = True
    planner = sep.SegmentEncodePlan(timebase=TB, segment_duration=1.0)
    segments = planner.plan(frames)
    assert len(segments) == 3
    assert segments[1]["keyframes"] == [30]
    assert sum(len(s["keyframes"]) for s in segments) == 3


def test_timeline_keeps_decode_and_display_times_separate():
    frames = [
        {"dts": 0, "pts": 0, "duration": 3000},
        {"dts": 1, "pts": 2, "duration": 3000},
        {"dts": 2, "pts": 1, "duration": 3000},
    ]
    planner = sep.SegmentEncodePlan(timebase=TB)
    samples = planner.timeline(frames)
    assert [s["dts"] for s in samples] == [0, 1, 2]
    assert samples[1]["pts"] == 2
    assert samples[2]["pts"] == 1


def test_non_monotonic_dts_is_rejected():
    frames = [
        {"dts": 0, "pts": 0, "duration": 3000},
        {"dts": 2, "pts": 6000, "duration": 3000},
        {"dts": 1, "pts": 3000, "duration": 3000},
    ]
    planner = sep.SegmentEncodePlan(timebase=TB)
    with pytest.raises(ValueError):
        planner.timeline(frames)


def test_segment_length_follows_time_not_frame_count():
    durations = [1000, 2000, 4000, 5000]
    frames = []
    t = 0
    for i in range(100):
        d = durations[i % 4]
        frames.append({"dts": t, "pts": t, "duration": d})
        t += d
    planner = sep.SegmentEncodePlan(timebase=TB, segment_duration=0.2)
    segments = planner.plan(frames)
    counts = [s["frame_range"][1] - s["frame_range"][0] for s in segments]
    assert len(segments) > 10
    assert len(set(counts)) > 1
    assert all(18000 <= s["duration_ticks"] <= 23000 for s in segments[:-1])
    assert segments[-1]["duration_ticks"] > 0


def test_short_last_segment_is_not_dropped():
    planner = sep.SegmentEncodePlan(timebase=TB, segment_duration=1.0)
    segments = planner.plan(uniform_frames(65))
    assert len(segments) == 3
    assert segments[-1]["frame_range"] == (60, 65)
    assert segments[-1]["end_pts"] == 195000
    assert segments[-1]["duration_ticks"] == 15000


def test_priming_is_applied_to_every_segment():
    planner = sep.SegmentEncodePlan(timebase=TB, segment_duration=1.0, priming=0.02)
    segments = planner.plan(uniform_frames(90))
    assert [s["priming"] for s in segments] == [1800, 1800, 1800]


def test_index_uses_media_timebase():
    planner = sep.SegmentEncodePlan(timebase=TB, segment_duration=1.0)
    segments = planner.plan(uniform_frames(90))
    index = planner.index(segments)
    assert [entry["segment"] for entry in index] == [0, 1, 2]
    assert [entry["time"] for entry in index] == [0.0, 1.0, 2.0]


def test_audio_boundaries_align_with_video_segments():
    planner = sep.SegmentEncodePlan(timebase=TB, audio_timebase=48000, segment_duration=2.0)
    segments = planner.plan(uniform_frames(180))
    audio = planner.audio_plan(segments)
    assert [a["start_pts"] for a in audio] == [0, 96000, 192000]
    assert [a["video_segment"] for a in audio] == [0, 1, 2]


def test_bitrate_budget_is_shared_across_workers():
    planner = sep.SegmentEncodePlan(timebase=TB, segment_duration=1.0, max_bitrate=8_000_000)
    segments = planner.plan(uniform_frames(90))
    bitrates = [s["bitrate"] for s in segments]
    assert sum(bitrates) == 8_000_000
    assert all(b > 0 for b in bitrates)
    assert max(bitrates) - min(bitrates) <= 1


def test_rate_control_debt_is_reset_per_segment():
    planner = sep.SegmentEncodePlan(timebase=TB, segment_duration=1.0)
    segments = planner.plan(uniform_frames(90))
    assert [s["rc_debt"] for s in segments] == [0, 0, 0]


def test_worker_assignment_is_exactly_once_and_round_robin():
    planner = sep.SegmentEncodePlan(timebase=TB, segment_duration=1.0, workers=3)
    segments = planner.plan(uniform_frames(210))
    assignment = planner.assign_workers(segments)
    assert len(assignment) == 7
    assert [a["segment"] for a in assignment] == [0, 1, 2, 3, 4, 5, 6]
    assert [a["worker"] for a in assignment] == [0, 1, 2, 0, 1, 2, 0]


def test_mux_writes_are_serialized_by_segment():
    planner = sep.SegmentEncodePlan(timebase=TB)
    writes = [
        {"segment": 2, "track": "audio", "offset": 3000, "size": 100},
        {"segment": 0, "track": "video", "offset": 0, "size": 500},
        {"segment": 1, "track": "audio", "offset": 1500, "size": 100},
        {"segment": 1, "track": "video", "offset": 0, "size": 400},
    ]
    ordered = planner.mux_order(writes)
    assert [w["segment"] for w in ordered] == [0, 1, 1, 2]
    assert ordered[1]["track"] == "video"
    assert ordered[2]["track"] == "audio"
    assert all(w["offset"] >= 0 for w in ordered)


def test_negative_mux_offset_is_rejected():
    planner = sep.SegmentEncodePlan(timebase=TB)
    with pytest.raises(ValueError):
        planner.mux_order([{"segment": 0, "track": "video", "offset": -1, "size": 10}])


def test_empty_input_produces_empty_plan():
    planner = sep.SegmentEncodePlan(timebase=TB)
    assert planner.plan([]) == []
    assert planner.index([]) == []
