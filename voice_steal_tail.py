"""Voice stealing with release envelopes, virtual voices and reverb tails."""


class VoiceStealPool:
    def __init__(self, max_voices=32, release_time=0.05, tail_time=1.0, reserved=None):
        self.max_voices = max_voices
        self.release_time = release_time
        self.tail_time = tail_time
        self.reserved = dict(reserved or {})
        self._next_id = 1
        self._next_generation = 1
        self.active = {}
        self.virtual = {}
        self.releasing = []
        self.tails = []

    def acquire(self, sound_id, priority=0, distance=0.0, duration=1.0,
                bus="sfx", loop=False, channels=2, fade_in=0.0, fade_out=0.0):
        raise NotImplementedError

    def release(self, handle, fade=0.02):
        raise NotImplementedError

    def virtualize(self, handle):
        raise NotImplementedError

    def restore(self, handle):
        raise NotImplementedError

    def advance(self, dt):
        raise NotImplementedError

    def is_active(self, handle):
        raise NotImplementedError

    def position(self, handle):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
