class TunnelMuxWindow:
    def __init__(self, connection_window=65536, stream_window=16384,
                 max_frame=16384, idle_timeout=30.0):
        self.connection_window = connection_window
        self.stream_window = stream_window
        self.max_frame = max_frame
        self.idle_timeout = idle_timeout

    def open_stream(self, stream_id, weight=1, now=0.0):
        raise NotImplementedError()

    def send(self, stream_id, size, now=0.0):
        raise NotImplementedError()

    def consume(self, stream_id, size, now=0.0):
        raise NotImplementedError()

    def close_stream(self, stream_id, direction):
        raise NotImplementedError()

    def reset_stream(self, stream_id):
        raise NotImplementedError()

    def state(self, stream_id):
        raise NotImplementedError()

    def on_data(self, stream_id, seq, payload, size=None, now=0.0):
        raise NotImplementedError()

    def deliverable(self, stream_id):
        raise NotImplementedError()

    def ack(self, stream_id, offset):
        raise NotImplementedError()

    def resume_offset(self, stream_id):
        raise NotImplementedError()

    def keepalive(self, now):
        raise NotImplementedError()

    def queue_control(self, size):
        raise NotImplementedError()

    def pending(self, stream_id, size):
        raise NotImplementedError()

    def schedule(self):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
