STATE_OPEN = "open"
STATE_HALF_CLOSED_LOCAL = "half_closed_local"
STATE_HALF_CLOSED_REMOTE = "half_closed_remote"
STATE_CLOSED = "closed"

SENDABLE_STATES = (STATE_OPEN, STATE_HALF_CLOSED_REMOTE)
RECEIVABLE_STATES = (STATE_OPEN, STATE_HALF_CLOSED_LOCAL)


class _Stream:
    def __init__(self, stream_id, weight, stream_window, now):
        self.stream_id = stream_id
        self.weight = weight
        self.window_limit = stream_window
        self.window = stream_window
        self.outstanding = 0
        self.state = STATE_OPEN
        self.sent_total = 0
        self.acked_offset = 0
        self.recv_next = 0
        self.reorder_buffer = {}
        self.delivered = []
        self.pending = 0
        self.last_activity = now


class TunnelMuxWindow:
    """Connection/stream flow-control windows, stream state machine and
    ordered reassembly for a multiplexed tunnel."""

    def __init__(self, connection_window=65536, stream_window=16384,
                 max_frame=16384, idle_timeout=30.0):
        if stream_window > connection_window:
            raise ValueError(
                "stream window (%d) must not exceed connection window (%d)"
                % (stream_window, connection_window))
        if connection_window <= 0 or stream_window <= 0 or max_frame <= 0:
            raise ValueError("window and frame limits must be positive")
        if idle_timeout <= 0:
            raise ValueError("idle_timeout must be positive")
        self.connection_window = connection_window
        self.stream_window = stream_window
        self.max_frame = max_frame
        self.idle_timeout = idle_timeout
        self.connection_remaining = connection_window
        self.streams = {}
        self.control_queue = []
        self._schedule_cycle = []
        self._schedule_cursor = 0

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _get_stream(self, stream_id):
        try:
            return self.streams[stream_id]
        except KeyError:
            raise ValueError("unknown stream: %r" % (stream_id,))

    def _rebuild_schedule_cycle(self):
        cycle = []
        for stream_id, stream in self.streams.items():
            if stream.state in SENDABLE_STATES:
                cycle.extend([stream_id] * stream.weight)
        self._schedule_cycle = cycle
        self._schedule_cursor = 0

    def _check_invariants(self):
        total_outstanding = 0
        for stream in self.streams.values():
            if stream.outstanding < 0:
                raise RuntimeError("negative outstanding window")
            if stream.window < 0 or stream.window > stream.window_limit:
                raise RuntimeError("stream window out of bounds")
            total_outstanding += stream.outstanding
        if self.connection_remaining < 0:
            raise RuntimeError("negative connection window")
        if total_outstanding > self.connection_window:
            raise RuntimeError("connection window exceeded")
        if self.connection_remaining != self.connection_window - total_outstanding:
            raise RuntimeError("connection window bookkeeping mismatch")

    # ------------------------------------------------------------------
    # stream lifecycle
    # ------------------------------------------------------------------
    def open_stream(self, stream_id, weight=1, now=0.0):
        if stream_id in self.streams:
            raise ValueError("stream %r already exists" % (stream_id,))
        if not isinstance(weight, int) or weight <= 0:
            raise ValueError("weight must be a positive integer")
        self.streams[stream_id] = _Stream(
            stream_id, weight, self.stream_window, now)
        self._rebuild_schedule_cycle()
        return STATE_OPEN

    def send(self, stream_id, size, now=0.0):
        stream = self._get_stream(stream_id)
        if stream.state not in SENDABLE_STATES:
            raise ValueError(
                "stream %r is not sendable in state %s"
                % (stream_id, stream.state))
        if size <= 0:
            raise ValueError("send size must be positive")
        if size > stream.window:
            raise ValueError(
                "stream window exhausted: need %d, have %d"
                % (size, stream.window))
        if size > self.connection_remaining:
            raise ValueError(
                "connection window exhausted: need %d, have %d"
                % (size, self.connection_remaining))
        stream.window -= size
        self.connection_remaining -= size
        stream.outstanding += size
        stream.sent_total += size
        stream.last_activity = now
        self._check_invariants()
        return {
            "stream": stream_id,
            "size": size,
            "stream_remaining": stream.window,
            "connection_remaining": self.connection_remaining,
        }

    def consume(self, stream_id, size, now=0.0):
        stream = self._get_stream(stream_id)
        if size <= 0:
            raise ValueError("consume size must be positive")
        if size > stream.outstanding:
            raise ValueError(
                "cannot consume %d bytes, outstanding is %d"
                % (size, stream.outstanding))
        stream.outstanding -= size
        stream.window += size
        self.connection_remaining += size
        stream.last_activity = now
        self._check_invariants()
        return {
            "stream": stream_id,
            "window_update": size,
            "connection_window_update": size,
            "stream_remaining": stream.window,
            "connection_remaining": self.connection_remaining,
        }

    def close_stream(self, stream_id, direction):
        stream = self._get_stream(stream_id)
        if direction == "local":
            if stream.state == STATE_OPEN:
                stream.state = STATE_HALF_CLOSED_LOCAL
            elif stream.state == STATE_HALF_CLOSED_REMOTE:
                stream.state = STATE_CLOSED
            else:
                raise ValueError(
                    "local direction of stream %r already closed"
                    % (stream_id,))
        elif direction == "remote":
            if stream.state == STATE_OPEN:
                stream.state = STATE_HALF_CLOSED_REMOTE
            elif stream.state == STATE_HALF_CLOSED_LOCAL:
                stream.state = STATE_CLOSED
            else:
                raise ValueError(
                    "remote direction of stream %r already closed"
                    % (stream_id,))
        else:
            raise ValueError("direction must be 'local' or 'remote'")
        self._rebuild_schedule_cycle()
        return stream.state

    def reset_stream(self, stream_id):
        stream = self._get_stream(stream_id)
        # Anything still in flight is released back to the connection window.
        self.connection_remaining += stream.outstanding
        stream.outstanding = 0
        stream.window = stream.window_limit
        stream.reorder_buffer.clear()
        stream.delivered.clear()
        stream.pending = 0
        stream.state = STATE_CLOSED
        self._rebuild_schedule_cycle()
        self._check_invariants()
        return STATE_CLOSED

    def state(self, stream_id):
        return self._get_stream(stream_id).state

    # ------------------------------------------------------------------
    # receive path: length check first, then in-order reassembly
    # ------------------------------------------------------------------
    def on_data(self, stream_id, seq, payload, size=None, now=0.0):
        stream = self._get_stream(stream_id)
        if stream.state not in RECEIVABLE_STATES:
            raise ValueError(
                "stream %r cannot receive data in state %s"
                % (stream_id, stream.state))
        if size is None:
            size = len(payload)
        if size <= 0:
            raise ValueError("frame size must be positive")
        # Validate the declared length before any allocation/buffering.
        if size > self.max_frame:
            raise ValueError(
                "frame size %d exceeds max_frame %d" % (size, self.max_frame))
        if seq < stream.recv_next:
            raise ValueError(
                "duplicate frame seq=%d, already through %d"
                % (seq, stream.recv_next))
        if seq in stream.reorder_buffer:
            raise ValueError("duplicate frame seq=%d" % (seq,))

        stream.last_activity = now
        if seq != stream.recv_next:
            stream.reorder_buffer[seq] = payload
            return "buffered"

        stream.delivered.append(payload)
        stream.recv_next += len(payload)
        while stream.recv_next in stream.reorder_buffer:
            buffered = stream.reorder_buffer.pop(stream.recv_next)
            stream.delivered.append(buffered)
            stream.recv_next += len(buffered)
        return "delivered"

    def deliverable(self, stream_id):
        stream = self._get_stream(stream_id)
        ready = stream.delivered
        stream.delivered = []
        return ready

    # ------------------------------------------------------------------
    # acknowledgements / reconnect resume
    # ------------------------------------------------------------------
    def ack(self, stream_id, offset):
        stream = self._get_stream(stream_id)
        if offset < 0 or offset > stream.sent_total:
            raise ValueError(
                "ack offset %d beyond sent total %d"
                % (offset, stream.sent_total))
        if offset > stream.acked_offset:
            stream.acked_offset = offset
        return stream.acked_offset

    def resume_offset(self, stream_id):
        return self._get_stream(stream_id).acked_offset

    # ------------------------------------------------------------------
    # keepalive: only streams with no progress time out
    # ------------------------------------------------------------------
    def keepalive(self, now):
        timed_out = []
        for stream_id, stream in self.streams.items():
            if stream.state == STATE_CLOSED:
                continue
            if now - stream.last_activity > self.idle_timeout:
                timed_out.append(stream_id)
        return timed_out

    # ------------------------------------------------------------------
    # scheduling: control frames first, weighted fair queuing for data
    # ------------------------------------------------------------------
    def queue_control(self, size):
        if size <= 0:
            raise ValueError("control frame size must be positive")
        self.control_queue.append(size)
        return len(self.control_queue)

    def pending(self, stream_id, size):
        stream = self._get_stream(stream_id)
        if size <= 0:
            raise ValueError("pending size must be positive")
        stream.pending += size
        return stream.pending

    def schedule(self):
        if self.control_queue:
            return {"kind": "control", "size": self.control_queue.pop(0)}
        cycle = self._schedule_cycle
        if not cycle:
            return None
        for _ in range(len(cycle)):
            stream_id = cycle[self._schedule_cursor]
            self._schedule_cursor = (self._schedule_cursor + 1) % len(cycle)
            stream = self.streams.get(stream_id)
            if (stream is not None
                    and stream.state in SENDABLE_STATES
                    and stream.pending > 0):
                return {
                    "kind": "data",
                    "stream": stream_id,
                    "size": stream.pending,
                }
        return None

    # ------------------------------------------------------------------
    # consistent, isolated snapshot
    # ------------------------------------------------------------------
    def snapshot(self):
        return {
            "connection_window": self.connection_remaining,
            "connection_window_limit": self.connection_window,
            "max_frame": self.max_frame,
            "idle_timeout": self.idle_timeout,
            "control_queue": list(self.control_queue),
            "streams": {
                stream_id: {
                    "state": stream.state,
                    "window": stream.window,
                    "window_limit": stream.window_limit,
                    "outstanding": stream.outstanding,
                    "weight": stream.weight,
                    "sent": stream.sent_total,
                    "acked": stream.acked_offset,
                    "recv_next": stream.recv_next,
                    "buffered": dict(stream.reorder_buffer),
                    "deliverable": list(stream.delivered),
                    "pending": stream.pending,
                    "last_activity": stream.last_activity,
                }
                for stream_id, stream in self.streams.items()
            },
        }
