import pytest

import fixed_step_accumulator as fsa


def test_max_substeps_caps_single_frame_catch_up():
    clock = fsa.FixedStepAccumulator(step=1.0 / 60.0, max_substeps=5, max_frame_dt=10.0)
    ticks = clock.advance(1.0)
    assert len(ticks) == 5
    assert clock.sim_time() == pytest.approx(5.0 / 60.0)


def test_huge_frame_dt_is_clamped():
    clock = fsa.FixedStepAccumulator(step=1.0 / 60.0, max_substeps=100, max_frame_dt=0.25)
    ticks = clock.advance(5.0)
    assert len(ticks) == 15
    assert clock.sim_time() == pytest.approx(0.25)


def test_alpha_interpolates_between_fixed_steps():
    clock = fsa.FixedStepAccumulator(step=1.0 / 60.0)
    assert clock.advance(1.0 / 120.0) == []
    assert clock.alpha() == pytest.approx(0.5)
    clock.advance(1.0 / 120.0)
    assert clock.alpha() == pytest.approx(0.0, abs=1e-9)


def test_pause_does_not_accumulate_time():
    clock = fsa.FixedStepAccumulator(step=1.0 / 60.0)
    clock.pause()
    assert clock.advance(10.0) == []
    assert clock.alpha() == 0.0
    assert clock.sim_time() == 0.0


def test_resume_after_pause_starts_one_step():
    clock = fsa.FixedStepAccumulator(step=1.0 / 60.0)
    clock.pause()
    clock.advance(10.0)
    clock.resume()
    ticks = clock.advance(1.0 / 60.0)
    assert len(ticks) == 1
    assert clock.sim_time() == pytest.approx(1.0 / 60.0)


def test_time_scale_scales_simulated_time():
    clock = fsa.FixedStepAccumulator(step=1.0 / 60.0, max_substeps=100, max_frame_dt=10.0)
    clock.set_time_scale(0.3)
    ticks = clock.advance(1.0)
    assert len(ticks) == 18
    assert clock.sim_time() == pytest.approx(0.3)
    assert all(t["dt"] == pytest.approx(1.0 / 60.0) for t in ticks)


def test_invalid_time_scale_is_rejected():
    clock = fsa.FixedStepAccumulator()
    with pytest.raises(ValueError):
        clock.set_time_scale(0.0)
    with pytest.raises(ValueError):
        clock.set_time_scale(-0.5)


def test_long_run_does_not_drift():
    clock = fsa.FixedStepAccumulator(step=1.0 / 60.0, max_substeps=2)
    for _ in range(36000):
        clock.advance(1.0 / 60.0)
    assert clock.tick() == 36000
    assert clock.sim_time() == pytest.approx(600.0, abs=1e-9)


def test_same_tick_events_are_ordered_deterministically():
    first = fsa.FixedStepAccumulator(step=1.0 / 60.0, max_substeps=100, max_frame_dt=10.0)
    first.schedule_event(0.5, "boom")
    first.schedule_event(0.5, "roar")
    first_events = [event for tick in first.advance(1.0) for event in tick["events"]]
    second = fsa.FixedStepAccumulator(step=1.0 / 60.0, max_substeps=100, max_frame_dt=10.0)
    second.schedule_event(0.5, "boom")
    second.schedule_event(0.5, "roar")
    second_events = [event for tick in second.advance(1.0) for event in tick["events"]]
    assert first_events == ["boom", "roar"]
    assert first_events == second_events


def test_events_fire_once():
    clock = fsa.FixedStepAccumulator(step=1.0 / 60.0, max_substeps=100, max_frame_dt=10.0)
    clock.schedule_event(0.5, "once")
    first = [event for tick in clock.advance(1.0) for event in tick["events"]]
    second = [event for tick in clock.advance(1.0) for event in tick["events"]]
    assert first == ["once"]
    assert second == []


def test_long_step_is_split_into_bounded_substeps():
    clock = fsa.FixedStepAccumulator(step=0.05, max_substep=0.01, max_substeps=10)
    ticks = clock.advance(0.05)
    assert len(ticks) == 5
    assert all(t["dt"] == pytest.approx(0.01) for t in ticks)
    assert clock.sim_time() == pytest.approx(0.05)


def test_snapshot_is_stable_across_advance():
    clock = fsa.FixedStepAccumulator(step=1.0 / 60.0)
    before = clock.snapshot()
    clock.advance(1.0 / 60.0)
    assert before == {"tick": 0, "sim_time": 0.0, "alpha": 0.0, "paused": False}
    assert clock.snapshot()["tick"] == 1


def test_correction_is_smoothed():
    clock = fsa.FixedStepAccumulator()
    remaining = 1.0
    applied = clock.correct(remaining)
    assert applied == pytest.approx(0.1)
    remaining -= applied
    total = applied
    for _ in range(9):
        applied = clock.correct(remaining)
        total += applied
        remaining -= applied
    assert total == pytest.approx(1.0 - 0.9 ** 10)


def test_negative_dt_is_rejected():
    clock = fsa.FixedStepAccumulator()
    with pytest.raises(ValueError):
        clock.advance(-0.01)


def test_zero_dt_returns_no_ticks_and_keeps_alpha():
    clock = fsa.FixedStepAccumulator()
    clock.advance(1.0 / 120.0)
    alpha = clock.alpha()
    assert clock.advance(0.0) == []
    assert clock.alpha() == alpha
