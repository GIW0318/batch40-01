class MusicStreamRing:
    def __init__(self, capacity_frames=48000, channels=2, device_rate=48000,
                 guard_frames=64):
        self.capacity_frames = capacity_frames
        self.channels = channels
        self.device_rate = device_rate
        self.guard_frames = guard_frames

    def capacity(self):
        raise NotImplementedError

    def available(self):
        raise NotImplementedError

    def free(self):
        raise NotImplementedError

    def write(self, samples):
        raise NotImplementedError

    def read(self, frames):
        raise NotImplementedError

    def seek(self, frame):
        raise NotImplementedError

    def begin_decode(self, track_id):
        raise NotImplementedError

    def decode_write(self, token, samples):
        raise NotImplementedError

    def end_decode(self, token):
        raise NotImplementedError

    def set_device_rate(self, rate):
        raise NotImplementedError

    def capacity_for(self, rate, seconds=1.0):
        raise NotImplementedError

    def trim_priming(self, samples, priming_frames):
        raise NotImplementedError

    def underrun(self, frames):
        raise NotImplementedError

    def crossfade(self, first, first_rate, second, second_rate, duration_frames):
        raise NotImplementedError

    def enqueue(self, track_id, priority):
        raise NotImplementedError

    def next_prefetch(self):
        raise NotImplementedError

    def release_track(self, track_id):
        raise NotImplementedError

    def allocate_pool(self, lengths):
        raise NotImplementedError

    def total_written(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
