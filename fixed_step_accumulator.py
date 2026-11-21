"""Fixed-step accumulator with interpolation, clamping and deterministic events."""

from fractions import Fraction
from math import isfinite


_FRACTION_LIMIT = 1_000_000_000


def _as_fraction(value, *, name):
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a number")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError(f"{name} must be finite")
    try:
        number = Fraction(value).limit_denominator(_FRACTION_LIMIT)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        raise TypeError(f"{name} must be a number") from exc
    return number


def _positive_fraction(value, *, name):
    number = _as_fraction(value, name=name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _non_negative_fraction(value, *, name):
    number = _as_fraction(value, name=name)
    if number < 0:
        raise ValueError(f"{name} must not be negative")
    return number


class FixedStepAccumulator:
    def __init__(self, step=1.0 / 60.0, max_substeps=5, max_frame_dt=0.25,
                 time_scale=1.0, max_substep=None):
        if isinstance(max_substeps, bool) or not isinstance(max_substeps, int):
            raise TypeError("max_substeps must be an integer")
        if max_substeps < 1:
            raise ValueError("max_substeps must be positive")

        configured_step = _positive_fraction(step, name="step")
        self._step = configured_step
        self._max_frame_dt = (
            None if max_frame_dt is None
            else _non_negative_fraction(max_frame_dt, name="max_frame_dt")
        )
        self._max_substep = (
            None if max_substep is None
            else _positive_fraction(max_substep, name="max_substep")
        )
        self._time_scale = _positive_fraction(time_scale, name="time_scale")
        if self._max_substep is not None:
            self._step = min(self._step, self._max_substep)

        self.step = float(configured_step)
        self.max_substeps = max_substeps
        self.max_frame_dt = (
            None if max_frame_dt is None else float(self._max_frame_dt)
        )
        self.time_scale = float(self._time_scale)
        self.max_substep = max_substep
        self._accumulator = Fraction(0)
        self.accumulator = 0.0
        self._sim_time = Fraction(0)
        self.paused = False
        self.fixed_steps = 0
        self.events = []
        self._next_event_sequence = 0

    def advance(self, dt):
        frame_dt = _non_negative_fraction(dt, name="dt")
        if self.paused:
            self._accumulator = Fraction(0)
            self.accumulator = 0.0
            return []

        if self._max_frame_dt is not None:
            frame_dt = min(frame_dt, self._max_frame_dt)
        frame_dt *= self._time_scale
        self._accumulator += frame_dt

        ticks = []
        while self._accumulator >= self._step:
            if len(ticks) >= self.max_substeps:
                self._accumulator %= self._step
                break
            self._accumulator -= self._step
            ticks.append(self._run_step())

        self.accumulator = float(self._accumulator)
        return ticks

    def alpha(self):
        if self.paused or self._step <= 0:
            return 0.0
        return float(self._accumulator / self._step)

    def pause(self):
        self.paused = True
        self._accumulator = Fraction(0)
        self.accumulator = 0.0

    def resume(self):
        if self.paused:
            self.paused = False
            self._accumulator = Fraction(0)
            self.accumulator = 0.0

    def set_time_scale(self, scale):
        self._time_scale = _positive_fraction(scale, name="time_scale")
        self.time_scale = float(self._time_scale)

    def sim_time(self):
        return float(self._sim_time)

    def tick(self):
        return self.fixed_steps

    def schedule_event(self, time_s, name):
        event_time = _non_negative_fraction(time_s, name="time_s")
        self.events.append({
            "time": event_time,
            "name": name,
            "sequence": self._next_event_sequence,
        })
        self._next_event_sequence += 1
        self.events.sort(key=lambda event: (event["time"], event["sequence"]))

    def snapshot(self):
        return {
            "tick": self.fixed_steps,
            "sim_time": self.sim_time(),
            "alpha": self.alpha(),
            "paused": self.paused,
        }

    def correct(self, error, smoothing=0.1):
        error = float(error)
        smoothing = float(smoothing)
        if not isfinite(error) or not isfinite(smoothing):
            raise ValueError("error and smoothing must be finite")
        if not 0.0 <= smoothing <= 1.0:
            raise ValueError("smoothing must be between 0 and 1")
        return error * smoothing

    def _run_step(self):
        self.fixed_steps += 1
        self._sim_time += self._step
        fired = []
        due = []
        remaining = []
        for event in self.events:
            if event["time"] <= self._sim_time:
                due.append(event)
            else:
                remaining.append(event)
        self.events = remaining
        for event in due:
            fired.append(event["name"])
        return {
            "tick": self.fixed_steps,
            "dt": float(self._step),
            "sim_time": float(self._sim_time),
            "events": fired,
        }
