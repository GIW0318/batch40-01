CONTINUATION_FRAME_OVERHEAD = 9


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
        self._streams = {}
        self._tenants = {}
        self._dependencies = {}
        self._goaway_last = None

    def _tenant(self, tenant):
        return self._tenants.setdefault(
            tenant, {"charged": 0, "queued": 0, "served": 0})

    def admit(self, tenant, stream_id, headers=b"", continuations=0,
              depends_on=None):
        if self._goaway_last is not None and stream_id > self._goaway_last:
            return {"admitted": False, "reason": "goaway"}
        header_bytes = len(headers) + continuations * CONTINUATION_FRAME_OVERHEAD
        if header_bytes > self.max_header_bytes:
            return {"admitted": False, "reason": "header_too_large"}
        self._tenant(tenant)
        active = sum(1 for s in self._streams.values()
                     if s["tenant"] == tenant)
        if active >= self.max_streams_per_tenant:
            return {"admitted": False, "reason": "stream_limit"}
        self._streams[stream_id] = {
            "tenant": tenant,
            "window": self.stream_window,
            "queued": 0,
            "served": 0,
        }
        if depends_on is not None and depends_on not in self._dependencies:
            depends_on = None
        self._dependencies[stream_id] = depends_on
        return {"admitted": True}

    def enqueue(self, stream_id, size, padding=0):
        stream = self._streams.get(stream_id)
        if stream is None:
            return {"accepted": False, "reason": "unknown_stream"}
        tenant = self._tenants[stream["tenant"]]
        total = size + padding
        if tenant["charged"] + total > self.tenant_quota:
            return {"accepted": False, "reason": "tenant_quota"}
        if tenant["queued"] + total > self.max_buffered:
            return {"accepted": False, "reason": "backpressure"}
        tenant["charged"] += total
        tenant["queued"] += total
        stream["queued"] += total
        return {"accepted": True}

    def schedule(self, max_bytes):
        budget = min(max_bytes, self.connection_window)
        allocations = []
        while budget > 0:
            eligible = {}
            for stream_id, stream in self._streams.items():
                if stream["queued"] > 0 and stream["window"] > 0:
                    eligible.setdefault(stream["tenant"], []).append(stream_id)
            if not eligible:
                break
            tenant = min(eligible,
                         key=lambda t: self._tenants[t]["served"])
            stream_id = min(eligible[tenant],
                            key=lambda sid: self._streams[sid]["served"])
            stream = self._streams[stream_id]
            chunk = min(self.quantum, stream["queued"], stream["window"],
                        budget)
            if chunk <= 0:
                break
            stream["queued"] -= chunk
            stream["window"] -= chunk
            stream["served"] += chunk
            tenant_state = self._tenants[tenant]
            tenant_state["queued"] -= chunk
            tenant_state["charged"] -= chunk
            tenant_state["served"] += chunk
            self.connection_window -= chunk
            budget -= chunk
            allocations.append((stream_id, chunk))
        return allocations

    def on_window(self, stream_id=None, delta=0, connection=False):
        if connection or stream_id is None:
            self.connection_window += delta
            return {"connection_window": self.connection_window}
        stream = self._streams.get(stream_id)
        if stream is None:
            return {"stream_id": stream_id, "stream_window": 0,
                    "error": "unknown_stream"}
        stream["window"] += delta
        return {"stream_id": stream_id, "stream_window": stream["window"]}

    def reset(self, stream_id):
        stream = self._streams.pop(stream_id, None)
        released = 0
        if stream is not None:
            released = stream["queued"]
            tenant = self._tenants[stream["tenant"]]
            tenant["queued"] -= released
            tenant["charged"] -= released
        self._dependencies.pop(stream_id, None)
        for child, parent in list(self._dependencies.items()):
            if parent == stream_id:
                self._dependencies[child] = None
        return {"stream_id": stream_id, "released": released}

    def goaway(self, last_stream_id):
        self._goaway_last = last_stream_id
        return {"goaway": True, "last_stream_id": last_stream_id}

    def stats(self):
        return {
            "connection_window": self.connection_window,
            "streams": len(self._streams),
            "tenants": {tenant: dict(state)
                        for tenant, state in self._tenants.items()},
            "dependencies": dict(self._dependencies),
        }
