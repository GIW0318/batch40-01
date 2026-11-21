class FlowFairness:
    """HTTP/2 flow-control and per-tenant fairness scheduler."""

    def __init__(self, connection_window=65535, stream_window=65535,
                 max_streams_per_tenant=100, tenant_quota=1048576,
                 max_buffered=262144, max_header_bytes=16384, quantum=10):
        self.connection_window = connection_window
        self.stream_window = stream_window
        self.max_streams_per_tenant = max_streams_per_tenant
        self.tenant_quota = tenant_quota
        self.max_buffered = max_buffered
        self.max_header_bytes = max_header_bytes
        self.quantum = quantum

    def admit(self, tenant, stream_id, headers=b"", continuations=0,
              depends_on=None):
        raise NotImplementedError

    def enqueue(self, stream_id, size, padding=0):
        raise NotImplementedError

    def schedule(self, max_bytes):
        raise NotImplementedError

    def on_window(self, stream_id=None, delta=0, connection=False):
        raise NotImplementedError

    def reset(self, stream_id):
        raise NotImplementedError

    def goaway(self, last_stream_id):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
