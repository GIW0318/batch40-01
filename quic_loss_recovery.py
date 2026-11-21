import threading


class _PathState:
    """Per-path congestion state so migration never inherits an old path."""

    def __init__(self, path_id, initial_cwnd):
        self.path_id = path_id
        self.cwnd = initial_cwnd


class _SentPacket:
    def __init__(self, number, sent_time, size, path_id, ack_only,
                 packet_type):
        self.number = number
        self.sent_time = sent_time
        self.size = size
        self.path_id = path_id
        self.ack_only = ack_only
        self.packet_type = packet_type
        self.acked = False
        self.lost = False


class QuicLossTracker:
    """QUIC loss recovery, congestion control and pacing bookkeeping."""

    def __init__(self, packet_threshold=3, time_threshold=1.25,
                 max_amplification=3.0, initial_cwnd=12000):
        self.packet_threshold = packet_threshold
        self.time_threshold = time_threshold
        self.max_amplification = max_amplification
        self.initial_cwnd = initial_cwnd

        self._lock = threading.RLock()

        # Sent-packet ledger, keyed by packet number.
        self._sent = {}
        self._in_flight = 0
        self._largest_acked = None

        # Statistics visible on the operations dashboard.
        self._stat_sent = 0
        self._stat_acked = 0
        self._stat_lost = 0
        self._stream_retransmits = 0

        # RTT estimators and PTO backoff.
        self._smoothed_rtt = None
        self._rttvar = None
        self._pto_count = 0

        # Address validation / anti-amplification.
        self._address_validated = False
        self._bytes_received = 0
        self._bytes_sent = 0

        # Per-path congestion windows.
        self._paths = {0: _PathState(0, initial_cwnd)}
        self._current_path = 0

        # Stream frame ledger for loss-driven retransmission.
        self._streams = {}

        # Token-bucket pacer.
        self._pacer_rate = None
        self._pacer_burst = None
        self._pacer_tokens = 0.0
        self._pacer_last_time = None

        # Key generations never invalidate 1-RTT packets in flight.
        self._key_generation = 0

    # ------------------------------------------------------------------
    # RTT and PTO
    # ------------------------------------------------------------------
    def on_rtt_sample(self, rtt):
        with self._lock:
            if self._smoothed_rtt is None:
                self._smoothed_rtt = float(rtt)
                self._rttvar = float(rtt) / 2.0
            else:
                rtt = float(rtt)
                self._rttvar = (3.0 * self._rttvar +
                                abs(self._smoothed_rtt - rtt)) / 4.0
                self._smoothed_rtt = (7.0 * self._smoothed_rtt + rtt) / 8.0

    def _loss_delay(self):
        if self._smoothed_rtt is None or self._rttvar is None:
            return None
        return self.time_threshold * max(self._smoothed_rtt,
                                         self._rttvar)

    def pto_interval(self):
        with self._lock:
            if self._smoothed_rtt is None or self._rttvar is None:
                base = 1000.0
            else:
                base = self._smoothed_rtt + 4.0 * self._rttvar
            return base * (2 ** self._pto_count)

    def on_pto_timeout(self, now):
        with self._lock:
            self._pto_count += 1

    # ------------------------------------------------------------------
    # Address validation and anti-amplification
    # ------------------------------------------------------------------
    def on_bytes_received(self, size):
        with self._lock:
            self._bytes_received += size

    def on_address_validated(self):
        with self._lock:
            self._address_validated = True

    def send_allowance(self):
        with self._lock:
            if self._address_validated:
                return None
            budget = int(self.max_amplification * self._bytes_received)
            return max(0, budget - self._bytes_sent)

    def can_send(self, size):
        with self._lock:
            if self._address_validated:
                return True
            budget = int(self.max_amplification * self._bytes_received)
            return self._bytes_sent + size <= budget

    # ------------------------------------------------------------------
    # Send / ack ledger
    # ------------------------------------------------------------------
    def on_packet_sent(self, number, now, size, path_id=0, ack_only=False,
                       packet_type="1rtt"):
        with self._lock:
            if not self.can_send(size):
                raise ValueError(
                    "sending %d bytes would exceed the anti-amplification "
                    "limit before address validation" % size)
            packet = _SentPacket(number, now, size, path_id, ack_only,
                                 packet_type)
            self._sent[number] = packet
            self._bytes_sent += size
            self._stat_sent += 1
            if not ack_only:
                self._in_flight += size

    def on_ack(self, number, now):
        with self._lock:
            packet = self._sent.get(number)
            if packet is None or packet.acked:
                return False

            packet.acked = True
            self._stat_acked += 1
            if not packet.ack_only:
                self._in_flight = max(0, self._in_flight - packet.size)

            if self._largest_acked is None or number > self._largest_acked:
                self._largest_acked = number

            path = self._paths.get(packet.path_id)
            if path is not None and not packet.ack_only:
                path.cwnd += packet.size

            # Any newly acknowledged packet proves progress: reset backoff.
            self._pto_count = 0
            return True

    def _mark_lost(self, packet):
        packet.lost = True
        if not packet.ack_only:
            self._in_flight = max(0, self._in_flight - packet.size)
        self._stat_lost += 1

    def detect_lost(self, now):
        with self._lock:
            if self._largest_acked is None:
                return []

            loss_delay = self._loss_delay()

            lost_numbers = []
            candidates = [packet
                          for packet in self._sent.values()
                          if not packet.acked and not packet.lost
                          and packet.number < self._largest_acked]
            candidates.sort(key=lambda packet: packet.number)

            for packet in candidates:
                threshold_number = packet.number + self.packet_threshold
                packet_threshold_hit = threshold_number <= self._largest_acked
                time_threshold_hit = (
                    loss_delay is not None
                    and now > packet.sent_time + loss_delay)
                if packet_threshold_hit or time_threshold_hit:
                    self._mark_lost(packet)
                    lost_numbers.append(packet.number)
            return lost_numbers

    # ------------------------------------------------------------------
    # Connection migration
    # ------------------------------------------------------------------
    def on_path_migration(self, path_id, now):
        with self._lock:
            if path_id not in self._paths:
                self._paths[path_id] = _PathState(path_id,
                                                   self.initial_cwnd)
            self._current_path = path_id

    def path_cwnd(self, path_id):
        with self._lock:
            path = self._paths.get(path_id)
            if path is None:
                return self.initial_cwnd
            return path.cwnd

    # ------------------------------------------------------------------
    # Stream retransmission
    # ------------------------------------------------------------------
    def write_stream_frame(self, stream_id, offset, data, fin=False):
        with self._lock:
            self._streams[stream_id] = {
                "stream_id": stream_id,
                "offset": offset,
                "data": data,
                "fin": fin,
            }

    def on_stream_lost(self, stream_id):
        with self._lock:
            frame = self._streams[stream_id]
            self._stream_retransmits += 1
            return {
                "stream_id": frame["stream_id"],
                "offset": frame["offset"],
                "data": frame["data"],
                "fin": frame["fin"],
            }

    # ------------------------------------------------------------------
    # Pacing (token bucket)
    # ------------------------------------------------------------------
    def configure_pacer(self, rate, burst):
        with self._lock:
            self._pacer_rate = float(rate)
            self._pacer_burst = float(burst)
            self._pacer_tokens = float(burst)
            self._pacer_last_time = None

    def allow_send(self, now, size):
        with self._lock:
            if self._pacer_rate is None:
                return True
            if self._pacer_last_time is None:
                self._pacer_last_time = now
            elapsed = max(0.0, now - self._pacer_last_time)
            self._pacer_last_time = now
            self._pacer_tokens = min(
                self._pacer_burst,
                self._pacer_tokens + elapsed * self._pacer_rate)
            if self._pacer_tokens >= size:
                self._pacer_tokens -= size
                return True
            return False

    # ------------------------------------------------------------------
    # Key updates
    # ------------------------------------------------------------------
    def on_key_update(self):
        with self._lock:
            self._key_generation += 1

    # ------------------------------------------------------------------
    # Thread-safe snapshot
    # ------------------------------------------------------------------
    def snapshot(self):
        with self._lock:
            return {
                "in_flight": self._in_flight,
                "sent": self._stat_sent,
                "acked": self._stat_acked,
                "lost": self._stat_lost,
                "pto_count": self._pto_count,
                "stream_retransmits": self._stream_retransmits,
                "current_path": self._current_path,
                "key_generation": self._key_generation,
                "paths": [
                    {"path_id": path.path_id, "cwnd": path.cwnd}
                    for path in sorted(self._paths.values(),
                                       key=lambda path: path.path_id)
                ],
            }
