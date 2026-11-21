class TcpStreamRebuilder:
    def __init__(self, gap_timeout=1.0, per_flow_limit=65536, global_limit=262144):
        self.gap_timeout = gap_timeout
        self.per_flow_limit = per_flow_limit
        self.global_limit = global_limit

    def add_segment(self, flow_key, seq, payload, flags="", urgent=0, ts=0.0, now=None):
        raise NotImplementedError()

    def read(self, flow_key, now=0.0):
        raise NotImplementedError()

    def flow_info(self, flow_key):
        raise NotImplementedError()

    def stats(self):
        raise NotImplementedError()

    def evict(self, now, max_idle=60.0):
        raise NotImplementedError()

    def parse_packet(self, raw):
        raise NotImplementedError()
