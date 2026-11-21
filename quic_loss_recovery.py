class QuicLossTracker:
    def __init__(self, packet_threshold=3, time_threshold=1.25,
                 max_amplification=3.0, initial_cwnd=12000):
        self.packet_threshold = packet_threshold
        self.time_threshold = time_threshold
        self.max_amplification = max_amplification
        self.initial_cwnd = initial_cwnd

    def on_rtt_sample(self, rtt):
        raise NotImplementedError()

    def on_bytes_received(self, size):
        raise NotImplementedError()

    def on_address_validated(self):
        raise NotImplementedError()

    def send_allowance(self):
        raise NotImplementedError()

    def can_send(self, size):
        raise NotImplementedError()

    def on_packet_sent(self, number, now, size, path_id=0, ack_only=False,
                       packet_type="1rtt"):
        raise NotImplementedError()

    def on_ack(self, number, now):
        raise NotImplementedError()

    def detect_lost(self, now):
        raise NotImplementedError()

    def pto_interval(self):
        raise NotImplementedError()

    def on_pto_timeout(self, now):
        raise NotImplementedError()

    def on_path_migration(self, path_id, now):
        raise NotImplementedError()

    def path_cwnd(self, path_id):
        raise NotImplementedError()

    def write_stream_frame(self, stream_id, offset, data, fin=False):
        raise NotImplementedError()

    def on_stream_lost(self, stream_id):
        raise NotImplementedError()

    def configure_pacer(self, rate, burst):
        raise NotImplementedError()

    def allow_send(self, now, size):
        raise NotImplementedError()

    def on_key_update(self):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
