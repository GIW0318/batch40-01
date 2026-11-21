"""Batch loudness normalization with gating, channel weights and checkpoints."""


class LoudnessBatch:
    def __init__(self, target_lufs=-16.0, tolerance=0.1, true_peak_limit=-1.0,
                 window_seconds=0.4):
        self.target_lufs = target_lufs
        self.tolerance = tolerance
        self.true_peak_limit = true_peak_limit
        self.window_seconds = window_seconds

    def integrated(self, blocks):
        raise NotImplementedError

    def gain_for(self, measured_lufs, peak_db=None, target_lufs=None, layout="stereo"):
        raise NotImplementedError

    def channel_weight(self, layout):
        raise NotImplementedError

    def channel_correction(self, layout):
        raise NotImplementedError

    def plan(self, items, mode="single"):
        raise NotImplementedError

    def output_names(self, names):
        raise NotImplementedError

    def merge_metadata(self, original, updated):
        raise NotImplementedError

    def checkpoint(self, done, total):
        raise NotImplementedError

    def resume(self, items, done):
        raise NotImplementedError
