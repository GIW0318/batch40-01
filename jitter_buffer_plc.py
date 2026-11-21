class JitterBuffer:
    def __init__(self, frame_ms=20, max_delay_ms=200, sample_rate=48000,
                 capacity_packets=None):
        self.frame_ms = frame_ms
        self.max_delay_ms = max_delay_ms
        self.sample_rate = sample_rate
        self.max_packets = capacity_packets

    def frame_samples(self):
        raise NotImplementedError

    def capacity_packets(self):
        raise NotImplementedError

    def sequence_newer(self, first, second):
        raise NotImplementedError

    def push(self, seq, samples, sample_rate=None, fec=False):
        raise NotImplementedError

    def pop(self):
        raise NotImplementedError

    def conceal(self, frames=1):
        raise NotImplementedError

    def set_target_delay(self, ms):
        raise NotImplementedError

    def adapt_delay(self, loss_rate, jitter_ms):
        raise NotImplementedError

    def set_vad_threshold(self, threshold):
        raise NotImplementedError

    def is_voice_active(self, rms):
        raise NotImplementedError

    def resample(self, samples, from_rate, to_rate):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
