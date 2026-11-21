"""IPv4/IPv6 fragment reassembly with overlap and resource policy.

Overlap policy is fixed to "first_wins": when fragments cover the same
byte range, the fragment closest to the start of the datagram (and for a
tie, the one that arrived first) keeps its data.  Reassembly is based on
byte intervals rather than naïve concatenation, so the same content is
delivered regardless of arrival order, keeping middle-box inspection
consistent with the end host.
"""

MAX_IP_OFFSET = 8191            # 13-bit fragment offset field
IP_OFFSET_UNIT = 8              # offset is expressed in 8-byte blocks
MAX_IP_PAYLOAD = 65535          # largest payload an IP datagram can carry


class _ReassemblyQueue:
    """Partially reassembled datagram identified by the 5-tuple key."""

    def __init__(self, key, seq, now):
        self.key = key
        self.seq = seq
        self.created = now
        self.last_seen = now
        self.version = key[0]
        self.ident = key[3]
        # Interval -> payload pieces: list of (start, end, data).
        self.pieces = []
        # Highest covered byte position; end of the fragment with MF=0.
        self.last_end = None
        self.declared_total = None
        # Protocol carried in the original (unfragmented) payload.
        self.proto = None
        self.fragment_count = 0
        self.duplicate_count = 0
        self.overlap_bytes = 0
        self.buffered_bytes = 0


class IpFragmentReassembler:
    """Reassembles datagrams from fragments and enforces queue limits."""

    def __init__(self, timeout=10.0, max_datagrams=64, max_fragments=64,
                 max_bytes=65536, per_source_limit=4):
        self.timeout = float(timeout)
        self.max_datagrams = max_datagrams
        self.max_fragments = max_fragments
        self.max_bytes = max_bytes
        self.per_source_limit = per_source_limit
        self.queues = {}
        self._seq = 0
        self._stats = {
            "completed": 0,
            "rejected": 0,
            "atomic": 0,
            "duplicates": 0,
            "overlaps": 0,
            "dropped_timeout": 0,
            "dropped_quota": 0,
            "dropped_memory": 0,
            "needs_fragmentation": 0,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def add(self, fragment, now):
        """Feed one fragment; return a result dict once complete else None."""
        self._validate_flags(fragment)

        offset_field = fragment["offset"]
        payload = fragment.get("payload", b"")
        if not isinstance(offset_field, int) or offset_field < 0:
            self._reject("fragment offset must be a non-negative integer")
        if offset_field > MAX_IP_OFFSET:
            self._reject("fragment offset %d exceeds 13-bit field" % offset_field)

        start = offset_field * IP_OFFSET_UNIT
        # Guard the offset*8 multiplication against integer overflow.
        if start > MAX_IP_PAYLOAD or start // IP_OFFSET_UNIT != offset_field:
            self._reject("byte offset %d overflows the datagram buffer" % start)

        end = start + len(payload)
        if end > MAX_IP_PAYLOAD:
            self._reject("fragment reaches byte %d beyond max payload" % end)

        key = (
            fragment["version"],
            fragment["src"],
            fragment["dst"],
            fragment["ident"],
            fragment.get("proto"),
        )
        queue = self.queues.get(key)
        # An atomic fragment (offset 0, MF=0) is only atomic when no
        # reassembly queue exists; if one does, this is the final fragment.
        is_atomic = offset_field == 0 and not fragment["mf"] and queue is None
        if is_atomic:
            return self._deliver_atomic(fragment, payload, now)

        if queue is None:
            self._reserve_slot(fragment["src"])
            self._seq += 1
            queue = _ReassemblyQueue(key, self._seq, now)
            self.queues[key] = queue
        queue.last_seen = now

        declared = fragment.get("total_length")
        if declared is not None:
            if not isinstance(declared, int) or declared < 0 or declared > MAX_IP_PAYLOAD:
                self._reject("declared total_length %d is invalid" % declared)
            if queue.declared_total is None:
                queue.declared_total = declared

        if queue.declared_total is not None and end > queue.declared_total:
            self._reject(
                "fragment end %d exceeds declared total_length %d"
                % (end, queue.declared_total)
            )

        duplicate = self._find_duplicate(queue, start, end)
        if duplicate is not None:
            queue.duplicate_count += 1
            self._stats["duplicates"] += 1
            return None

        if queue.fragment_count >= self.max_fragments:
            self._reject(
                "fragment count limit %d reached for datagram %s"
                % (self.max_fragments, queue.ident)
            )

        overlap = self._covered_bytes(queue, start, end)
        if overlap:
            queue.overlap_bytes += overlap
            self._stats["overlaps"] += overlap

        self._reserve_memory(queue, len(payload))

        queue.pieces.append((start, end, payload))
        queue.fragment_count += 1
        queue.buffered_bytes += len(payload)
        if offset_field == 0:
            queue.proto = self._resolve_proto(fragment)
        if not fragment["mf"]:
            queue.last_end = end

        return self._try_complete(queue, now)

    def tick(self, now):
        """Evict queues whose activity deadline passed; return eviction count."""
        expired = [
            key for key, queue in self.queues.items()
            if now - queue.last_seen >= self.timeout
        ]
        for key in expired:
            del self.queues[key]
            self._stats["dropped_timeout"] += 1
        return len(expired)

    def needs_fragmentation(self, src, dst, mtu, now):
        """Pass an ICMP/ICMPv6 fragmentation-needed message to upper layers."""
        self._stats["needs_fragmentation"] += 1
        return {
            "action": "notify_upper",
            "src": src,
            "dst": dst,
            "mtu": mtu,
            "now": now,
        }

    def stats(self):
        result = dict(self._stats)
        result["active"] = len(self.queues)
        return result

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    def _validate_flags(self, fragment):
        # DF set together with a fragment (MF or non-zero offset) is a
        # contradiction: a DF datagram must never be fragmented.
        if fragment.get("df") and (fragment["mf"] or fragment["offset"] != 0):
            self._reject("DF flag conflicts with fragmented datagram")

    def _reject(self, message):
        self._stats["rejected"] += 1
        raise ValueError(message)

    # ------------------------------------------------------------------
    # Resource management
    # ------------------------------------------------------------------
    def _source_counts(self):
        counts = {}
        for queue in self.queues.values():
            counts[queue.key[1]] = counts.get(queue.key[1], 0) + 1
        return counts

    def _oldest_queue(self):
        return min(self.queues.values(), key=lambda queue: queue.seq)

    def _evict_oldest(self, counter):
        queue = self._oldest_queue()
        del self.queues[queue.key]
        self._stats[counter] += 1

    def _reserve_slot(self, src):
        source_counts = self._source_counts()
        if source_counts.get(src, 0) >= self.per_source_limit:
            # Prefer evicting the oldest queue belonging to this source.
            victim = min(
                (queue for queue in self.queues.values() if queue.key[1] == src),
                key=lambda queue: queue.seq,
            )
            del self.queues[victim.key]
            self._stats["dropped_quota"] += 1
        if len(self.queues) >= self.max_datagrams:
            self._evict_oldest("dropped_quota")

    def _reserve_memory(self, current, extra):
        while self._buffered_bytes() + extra > self.max_bytes:
            candidates = [
                queue for queue in self.queues.values()
                if queue.key != current.key
            ]
            if not candidates:
                self._reject(
                    "fragment of %d bytes exceeds memory budget %d"
                    % (extra, self.max_bytes)
                )
            victim = min(candidates, key=lambda queue: queue.seq)
            del self.queues[victim.key]
            self._stats["dropped_memory"] += 1

    def _buffered_bytes(self):
        return sum(queue.buffered_bytes for queue in self.queues.values())

    # ------------------------------------------------------------------
    # Interval bookkeeping (deduplication + first-wins overlaps)
    # ------------------------------------------------------------------
    @staticmethod
    def _find_duplicate(queue, start, end):
        for piece_start, piece_end, _data in queue.pieces:
            if piece_start == start and piece_end == end:
                return (piece_start, piece_end)
        return None

    @staticmethod
    def _covered_bytes(queue, start, end):
        covered = 0
        for piece_start, piece_end, _data in queue.pieces:
            overlap_start = max(start, piece_start)
            overlap_end = min(end, piece_end)
            if overlap_end > overlap_start:
                covered += overlap_end - overlap_start
        return covered

    @staticmethod
    def _unioned_intervals(queue):
        intervals = sorted((start, end) for start, end, _data in queue.pieces)
        merged = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    # ------------------------------------------------------------------
    # Completion and delivery
    # ------------------------------------------------------------------
    def _resolve_proto(self, fragment):
        # For IPv6 the fragment header's next_header field identifies the
        # original payload; IPv4 carries the protocol number directly.
        if fragment.get("version") == 6 and "next_header" in fragment:
            return fragment["next_header"]
        return fragment.get("proto")

    def _try_complete(self, queue, now):
        if queue.last_end is None:
            return None
        intervals = self._unioned_intervals(queue)
        if len(intervals) != 1 or intervals[0][0] != 0:
            return None
        total = intervals[0][1]
        if total != queue.last_end:
            return None
        # The sum of the fragments must match the first fragment's
        # declared total_length; never deliver an inconsistent datagram.
        if queue.declared_total is not None and total != queue.declared_total:
            return None

        data = self._assemble(queue, total)
        del self.queues[queue.key]
        self._stats["completed"] += 1
        return {
            "complete": True,
            "atomic": False,
            "integrity": "complete",
            "version": queue.version,
            "src": queue.key[1],
            "dst": queue.key[2],
            "ident": queue.ident,
            "proto": queue.proto,
            "data": data,
            "total_length": total,
            "fragments": queue.fragment_count,
            "duplicates": queue.duplicate_count,
            "overlaps": queue.overlap_bytes,
            "policy": "first_wins",
            "reassembled_at": now,
        }

    @staticmethod
    def _assemble(queue, total):
        # Lowest offset wins; for identical offsets arrival order wins.
        ordered = sorted(queue.pieces, key=lambda piece: (piece[0], piece[1]))
        buffer = bytearray(total)
        filled = bytearray(total)
        for start, end, payload in ordered:
            for index in range(start, end):
                if not filled[index]:
                    buffer[index] = payload[index - start]
                    filled[index] = 1
        return bytes(buffer)

    def _deliver_atomic(self, fragment, payload, now):
        # Atomic fragments (offset 0, MF=0) are complete datagrams and
        # bypass the reassembly cache entirely (RFC 2460 section 4.5).
        self._stats["atomic"] += 1
        return {
            "complete": True,
            "atomic": True,
            "integrity": "complete",
            "version": fragment["version"],
            "src": fragment["src"],
            "dst": fragment["dst"],
            "ident": fragment["ident"],
            "proto": self._resolve_proto(fragment),
            "data": payload,
            "total_length": fragment.get("total_length", len(payload)),
            "fragments": 1,
            "duplicates": 0,
            "overlaps": 0,
            "policy": "first_wins",
            "reassembled_at": now,
        }
