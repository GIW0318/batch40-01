"""Cinematic timeline evaluation: overlaps, loops, seeks and nested sequences."""


class CinematicTrackEvaluator:
    def __init__(self, fps=60, ticks_per_second=1000):
        self.fps = fps
        self.ticks_per_second = ticks_per_second
        self.tracks = {}
        self.clips = []
        self.time_scale = 1.0
        self.fired = set()
        self.last_seek = 0.0
        self.hold = {}

    def add_track(self, name, prop, priority=0):
        raise NotImplementedError

    def add_clip(self, track, start, duration, value=None, loop=False,
                 priority=None, events=()):
        raise NotImplementedError

    def evaluate(self, t):
        raise NotImplementedError

    def resolve(self, t):
        raise NotImplementedError

    def events_between(self, t0, t1):
        raise NotImplementedError

    def seek(self, t):
        raise NotImplementedError

    def set_time_scale(self, scale):
        raise NotImplementedError

    def audio_rate(self):
        raise NotImplementedError

    def nested_time(self, child_t, offset, child_scale=1.0):
        raise NotImplementedError

    def disable_track(self, name, at):
        raise NotImplementedError

    def enable_track(self, name, at):
        raise NotImplementedError

    def track_time(self, name):
        raise NotImplementedError

    def to_ticks(self, seconds):
        raise NotImplementedError

    def sample_times(self, duration, fps):
        raise NotImplementedError
