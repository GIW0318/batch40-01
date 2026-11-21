class AudioBusGraph:
    def __init__(self, max_depth=4, sample_rate=48000, channels=2):
        self.max_depth = max_depth
        self.sample_rate = sample_rate
        self.channels = channels

    def add_bus(self, name, parent=None, gain=1.0):
        raise NotImplementedError

    def add_voice(self, voice_id, bus):
        raise NotImplementedError

    def remove_voice(self, voice_id):
        raise NotImplementedError

    def connect(self, bus, parent):
        raise NotImplementedError

    def validate(self):
        raise NotImplementedError

    def rebuild(self, routes=None, max_steps=None):
        raise NotImplementedError

    def graph_version(self):
        raise NotImplementedError

    def active_bus(self, name):
        raise NotImplementedError

    def set_gain(self, bus, gain, ramp_ms=100):
        raise NotImplementedError

    def gain_at(self, bus):
        raise NotImplementedError

    def tick(self, ms):
        raise NotImplementedError

    def set_channel_layout(self, layout):
        raise NotImplementedError

    def channel_count(self):
        raise NotImplementedError

    def set_sample_rate(self, rate):
        raise NotImplementedError

    def latency_samples(self):
        raise NotImplementedError

    def set_send(self, bus, target, amount=0.0, pre_fader=True):
        raise NotImplementedError

    def send_config(self, bus, target):
        raise NotImplementedError

    def snapshot(self, name, sends):
        raise NotImplementedError

    def apply_snapshot(self, snapshot):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
