class FlowRollup:
    def __init__(self, sampling_rate=1, active_timeout=300, idle_timeout=15,
                 bucket_seconds=300, retention=3600):
        self.sampling_rate = sampling_rate
        self.active_timeout = active_timeout
        self.idle_timeout = idle_timeout
        self.bucket_seconds = bucket_seconds
        self.retention = retention

    def ingest(self, record, now=None):
        raise NotImplementedError()

    def flows(self):
        raise NotImplementedError()

    def expire(self, now):
        raise NotImplementedError()

    def purge(self, now):
        raise NotImplementedError()

    def rollup(self, bucket_start):
        raise NotImplementedError()

    def top(self, n):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
