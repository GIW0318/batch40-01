import pytest

import cinematic_track_eval as cte


def test_explicit_clip_priority_wins_overlap():
    evaluator = cte.CinematicTrackEvaluator()
    evaluator.add_track("cam", "camera_x")
    evaluator.add_clip("cam", 0.0, 10.0, 1.0)
    evaluator.add_clip("cam", 5.0, 10.0, 2.0, priority=10)
    assert evaluator.evaluate(6.0)["camera_x"] == 2.0


def test_later_clip_wins_when_priorities_tie():
    evaluator = cte.CinematicTrackEvaluator()
    evaluator.add_track("cam", "camera_x")
    evaluator.add_clip("cam", 0.0, 10.0, 1.0)
    evaluator.add_clip("cam", 5.0, 10.0, 2.0)
    assert evaluator.evaluate(6.0)["camera_x"] == 2.0


def test_non_overlapping_clips_evaluate_independently():
    evaluator = cte.CinematicTrackEvaluator()
    evaluator.add_track("cam", "camera_x")
    evaluator.add_clip("cam", 0.0, 5.0, 1.0)
    evaluator.add_clip("cam", 5.0, 5.0, 2.0)
    assert evaluator.evaluate(2.0)["camera_x"] == 1.0
    assert evaluator.evaluate(7.0)["camera_x"] == 2.0


def test_loop_event_fires_once_per_loop_iteration():
    evaluator = cte.CinematicTrackEvaluator()
    evaluator.add_track("boss", "roar")
    evaluator.add_clip(
        "boss",
        0.0,
        2.0,
        1.0,
        loop=True,
        events=[{"at": 0.5, "name": "roar"}],
    )
    events = evaluator.events_between(0.0, 6.0)
    assert [event["name"] for event in events] == ["roar", "roar", "roar"]
    assert [event["time"] for event in events] == [0.5, 2.5, 4.5]


def test_event_on_query_boundary_is_not_duplicated():
    evaluator = cte.CinematicTrackEvaluator()
    evaluator.add_track("cam", "camera_x")
    evaluator.add_clip(
        "cam",
        0.0,
        10.0,
        1.0,
        events=[{"at": 1.0, "name": "cut"}],
    )
    first = evaluator.events_between(0.0, 1.0)
    second = evaluator.events_between(1.0, 2.0)
    assert first == []
    assert [event["name"] for event in second] == ["cut"]


def test_time_scale_affects_audio_and_event_mapping():
    evaluator = cte.CinematicTrackEvaluator()
    evaluator.add_track("cam", "camera_x")
    evaluator.add_clip(
        "cam",
        0.0,
        10.0,
        5.0,
        events=[{"at": 1.0, "name": "beat"}],
    )
    evaluator.set_time_scale(0.5)
    assert evaluator.audio_rate() == 0.5
    events = evaluator.events_between(1.5, 2.5)
    assert len(events) == 1
    assert events[0]["time"] == pytest.approx(2.0)


def test_seek_rebuilds_one_shot_events():
    evaluator = cte.CinematicTrackEvaluator()
    evaluator.add_track("cam", "camera_x")
    evaluator.add_clip(
        "cam",
        0.0,
        10.0,
        1.0,
        events=[{"at": 2.0, "name": "first"}, {"at": 4.0, "name": "second"}],
    )
    assert evaluator.seek(5.0) == ["first", "second"]
    assert evaluator.evaluate(6.0)["camera_x"] == 1.0


def test_repeated_seek_does_not_replay_events():
    evaluator = cte.CinematicTrackEvaluator()
    evaluator.add_track("cam", "camera_x")
    evaluator.add_clip(
        "cam",
        0.0,
        10.0,
        1.0,
        events=[{"at": 2.0, "name": "first"}, {"at": 4.0, "name": "second"}],
    )
    assert evaluator.seek(3.0) == ["first"]
    assert evaluator.seek(5.0) == ["second"]
    assert evaluator.seek(5.0) == []


def test_nested_sequence_timebase_conversion():
    evaluator = cte.CinematicTrackEvaluator()
    assert evaluator.nested_time(2.0, 10.0, 0.5) == pytest.approx(14.0)
    assert evaluator.nested_time(2.0, 10.0, 2.0) == pytest.approx(11.0)


def test_zero_length_clip_is_skipped_without_division_error():
    evaluator = cte.CinematicTrackEvaluator()
    evaluator.add_track("cam", "camera_x")
    evaluator.add_clip("cam", 0.0, 0.0, 9.0)
    evaluator.add_clip("cam", 0.0, 5.0, 1.0)
    assert evaluator.evaluate(0.0)["camera_x"] == 1.0


def test_conflicting_tracks_resolve_by_priority_not_order():
    evaluator = cte.CinematicTrackEvaluator()
    evaluator.add_track("low", "camera_x", priority=0)
    evaluator.add_clip("low", 0.0, 10.0, 1.0)
    evaluator.add_track("high", "camera_x", priority=10)
    evaluator.add_clip("high", 0.0, 10.0, 2.0)
    resolved = evaluator.resolve(1.0)
    assert resolved["values"]["camera_x"] == 2.0
    assert resolved["sources"]["camera_x"] == "high"


def test_events_on_the_same_boundary_have_stable_order():
    evaluator = cte.CinematicTrackEvaluator()
    evaluator.add_track("a", "p", priority=0)
    evaluator.add_clip("a", 0.0, 10.0, 1.0, events=[{"at": 2.0, "name": "a"}])
    evaluator.add_track("b", "q", priority=10)
    evaluator.add_clip("b", 0.0, 10.0, 1.0, events=[{"at": 2.0, "name": "b"}])
    first = [event["name"] for event in evaluator.events_between(1.0, 3.0)]
    second = [event["name"] for event in evaluator.events_between(1.0, 3.0)]
    assert first == ["b", "a"]
    assert first == second


def test_disabled_track_holds_time_and_resumes_without_jump():
    evaluator = cte.CinematicTrackEvaluator()
    evaluator.add_track("cam", "camera_x")
    evaluator.add_clip("cam", 0.0, 10.0, 3.0)
    evaluator.evaluate(1.0)
    evaluator.disable_track("cam", 1.0)
    evaluator.evaluate(5.0)
    assert evaluator.track_time("cam") == pytest.approx(1.0)
    evaluator.enable_track("cam", 5.0)
    assert evaluator.track_time("cam") == pytest.approx(1.0)
    evaluator.evaluate(6.0)
    assert evaluator.track_time("cam") == pytest.approx(2.0)


def test_preview_and_offline_frame_rates_agree():
    preview = cte.CinematicTrackEvaluator(fps=60)
    offline = cte.CinematicTrackEvaluator(fps=24)
    for evaluator in (preview, offline):
        evaluator.add_track("cam", "camera_x")
        evaluator.add_clip("cam", 0.0, 2.0, 4.0)
    samples = preview.sample_times(1.0, 24)
    assert len(samples) == 25
    assert samples[0] == 0.0
    assert samples[-1] == pytest.approx(1.0)
    for t in samples:
        assert preview.evaluate(t)["camera_x"] == offline.evaluate(t)["camera_x"]


def test_long_sequence_uses_exact_ticks_and_rejects_invalid_clip():
    evaluator = cte.CinematicTrackEvaluator(ticks_per_second=1000)
    assert evaluator.to_ticks(7200.0) == 7200000
    evaluator.add_track("cam", "camera_x")
    evaluator.add_clip("cam", 0.0, 10000.0, 7.0)
    assert evaluator.evaluate(7200.0)["camera_x"] == 7.0
    with pytest.raises(ValueError):
        evaluator.add_clip("cam", 0.0, -1.0, 1.0)
