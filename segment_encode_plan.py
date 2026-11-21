"""Segmented encoding planner: independent boundaries, ordered mux output."""


class SegmentEncodePlan:
    def __init__(self, timebase=90000, audio_timebase=48000, segment_duration=2.0,
                 gop_size=30, max_bitrate=8_000_000, workers=4, priming=0.02):
        self.timebase = timebase
        self.audio_timebase = audio_timebase
        self.segment_duration = segment_duration
        self.gop_size = gop_size
        self.max_bitrate = max_bitrate
        self.workers = workers
        self.priming = priming

    def plan(self, frames):
        raise NotImplementedError

    def timeline(self, frames):
        raise NotImplementedError

    def assign_workers(self, segments):
        raise NotImplementedError

    def audio_plan(self, segments):
        raise NotImplementedError

    def mux_order(self, writes):
        raise NotImplementedError

    def index(self, segments):
        raise NotImplementedError

    def to_ticks(self, seconds):
        raise NotImplementedError

    def to_seconds(self, ticks):
        raise NotImplementedError
