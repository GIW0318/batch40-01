import bisect
import struct


MOD = 1 << 32
HALF = 1 << 31

OVERLAP_POLICY = "first-wins"

ETHERTYPE_VLAN = 0x8100
ETHERTYPE_QINQ = 0x88A8
ETHERTYPE_IPV4 = 0x0800

TCP_FIN = 0x01
TCP_URG = 0x20


def seq_diff(seq, base):
    """Signed distance from ``base`` to ``seq`` in 32-bit sequence space.

    Handles wraparound: a value whose forward distance is larger than half
    the space is treated as belonging to the previous generation.
    """
    delta = (seq - base) % MOD
    if delta >= HALF:
        delta -= MOD
    return delta


class _Flow:
    def __init__(self):
        # Sorted disjoint [start, end) offsets waiting to be delivered.
        self.intervals = []
        # Every offset range ever covered, including delivered bytes. Used
        # for retransmission/overlap deduplication and never popped.
        self.covered = []
        # Payload bytes keyed by relative start offset.
        self.buffers = {}
        # Next contiguous relative offset expected for delivery.
        self.next_rel = 0
        # Lowest relative offset currently held in the delivered data.
        self.low_rel = 0
        # Absolute 32-bit sequence number corresponding to relative offset 0.
        self.base_seq = None
        # Delivered reassembled stream, kept so read() is idempotent.
        self.data = bytearray()
        # Gap markers emitted once the gap timeout elapsed.
        self.gaps = []
        # Arrival time at which the current hole was first observed.
        self.gap_since = None
        # Relative offset one past the FIN sequence number.
        self.fin_offset = None
        # Last advertised urgent pointer (absolute sequence space offset).
        self.urgent = 0
        self.last_arrival = 0.0
        self.segments = 0
        self.bytes = 0
        self.unique_bytes = 0


class TcpStreamRebuilder:
    def __init__(self, gap_timeout=1.0, per_flow_limit=65536, global_limit=262144):
        self.gap_timeout = gap_timeout
        self.per_flow_limit = per_flow_limit
        self.global_limit = global_limit
        self.flows = {}
        # Partial IP datagrams keyed by (src, dst, ident, protocol).
        self._fragments = {}

    # ------------------------------------------------------------------
    # TCP stream reassembly
    # ------------------------------------------------------------------
    def _to_rel(self, flow, seq):
        if flow.base_seq is None:
            flow.base_seq = seq & (MOD - 1)
        return seq_diff(seq, flow.base_seq)

    @staticmethod
    def _rebase(flow, shift):
        """Move the relative origin backwards by ``shift`` offsets."""
        flow.base_seq = (flow.base_seq - shift) & (MOD - 1)
        flow.buffers = {key + shift: value for key, value in flow.buffers.items()}
        flow.intervals = [
            (start + shift, end + shift) for start, end in flow.intervals
        ]
        flow.covered = [
            (start + shift, end + shift) for start, end in flow.covered
        ]
        flow.next_rel += shift
        if flow.data:
            flow.low_rel += shift
        if flow.fin_offset is not None:
            flow.fin_offset += shift

    @staticmethod
    def _uncovered_pieces(flow, start, length):
        """Split a new segment into pieces not already covered.

        Overlapping bytes keep the data that arrived first (first-wins).
        """
        end = start + length
        pieces = []
        cursor = start
        for cov_start, cov_end in flow.covered:
            if cov_end <= cursor:
                continue
            if cov_start >= end:
                break
            if cov_start > cursor:
                pieces.append((cursor, cov_start))
            if cov_end > cursor:
                cursor = cov_end
        if cursor < end:
            pieces.append((cursor, end))
        return pieces

    @staticmethod
    def _insert_interval(intervals, start, end):
        """Merge [start, end) into a sorted list of disjoint intervals."""
        keys = [item[0] for item in intervals]
        index = bisect.bisect_left(keys, start)
        while index > 0 and intervals[index - 1][1] >= start:
            index -= 1
        merged_start, merged_end = start, end
        while index < len(intervals) and intervals[index][0] <= merged_end:
            existing_start, existing_end = intervals.pop(index)
            merged_start = min(merged_start, existing_start)
            merged_end = max(merged_end, existing_end)
        intervals.insert(index, (merged_start, merged_end))

    def add_segment(self, flow_key, seq, payload, flags="", urgent=0,
                    ts=0.0, now=None):
        if now is None:
            now = ts
        payload = bytes(payload)
        flow = self.flows.get(flow_key)
        if flow is None:
            flow = _Flow()
            rel_start = 0
            flow.base_seq = seq & (MOD - 1)
        else:
            rel_start = self._to_rel(flow, seq)
            if rel_start < 0:
                self._rebase(flow, -rel_start)
                rel_start = 0

        pieces = self._uncovered_pieces(flow, rel_start, len(payload))
        added = sum(end - start for start, end in pieces)

        # Enforce limits before mutating any state so a rejected segment has
        # no effect on the stream.
        if added:
            if flow.unique_bytes + added > self.per_flow_limit:
                raise ValueError(
                    "per-flow buffer limit exceeded for flow %r" % (flow_key,)
                )
            total_buffered = sum(
                item.unique_bytes for item in self.flows.values()
            )
            if total_buffered + added > self.global_limit:
                raise ValueError(
                    "global buffer limit exceeded for flow %r" % (flow_key,)
                )

        is_new_flow = flow_key not in self.flows
        if is_new_flow:
            self.flows[flow_key] = flow

        flow.segments += 1
        flow.bytes += len(payload)
        if now > flow.last_arrival:
            flow.last_arrival = now

        if urgent:
            flow.urgent = urgent

        for piece_start, piece_end in pieces:
            offset = piece_start - rel_start
            flow.buffers[piece_start] = payload[offset:offset + piece_end - piece_start]
            self._insert_interval(
                flow.covered, piece_start, piece_end
            )
            self._insert_interval(
                flow.intervals, piece_start, piece_end
            )
            flow.unique_bytes += piece_end - piece_start

        if isinstance(flags, str):
            flag_fin = "FIN" in flags.upper()
        else:
            flag_fin = bool(flags & TCP_FIN)
        if flag_fin:
            fin_rel = rel_start + len(payload)
            if flow.fin_offset is None or fin_rel < flow.fin_offset:
                flow.fin_offset = fin_rel

        self._pump(flow, now)

    def _pump(self, flow, now):
        # Backward fill: close holes immediately before already delivered
        # data (happens when a lower-sequence segment arrives late).
        while flow.data:
            candidate = next(
                (
                    index
                    for index, (start, end) in enumerate(flow.intervals)
                    if end == flow.low_rel
                ),
                None,
            )
            if candidate is None:
                break
            start, end = flow.intervals.pop(candidate)
            piece = self._collect(flow, start, end)
            self._consume_buffers(flow, end)
            flow.data[:0] = piece
            flow.low_rel = start

        # Forward delivery from the contiguous cursor.
        while flow.intervals and flow.intervals[0][0] <= flow.next_rel:
            start, end = flow.intervals.pop(0)
            if end <= flow.next_rel:
                continue
            chunk_start = max(start, flow.next_rel)
            piece = self._collect(flow, chunk_start, end)
            self._consume_buffers(flow, end)
            flow.data.extend(piece)
            flow.next_rel = end
            flow.gap_since = None

        if flow.intervals:
            hole_start = flow.next_rel
            hole_end = flow.intervals[0][0]
            if hole_end > hole_start:
                if flow.gap_since is None:
                    flow.gap_since = now
                elif now - flow.gap_since >= self.gap_timeout:
                    flow.gaps.append({
                        "start": (flow.base_seq + hole_start) & (MOD - 1),
                        "end": (flow.base_seq + hole_end) & (MOD - 1),
                    })
                    flow.next_rel = hole_end
                    flow.gap_since = None
                    # Stop here: data unlocked by skipping the gap becomes
                    # visible on the next read/add so output stays stable.

    @staticmethod
    def _consume_buffers(flow, end):
        for key in [key for key in flow.buffers if key < end]:
            buf = flow.buffers.pop(key)
            if key + len(buf) > end:
                flow.buffers[end] = buf[end - key:]

    @staticmethod
    def _collect(flow, start, end):
        data = bytearray()
        for key in sorted(flow.buffers):
            piece = flow.buffers[key]
            piece_end = key + len(piece)
            if piece_end <= start:
                continue
            if key >= end:
                break
            data.extend(piece[max(0, start - key):end - key])
        return bytes(data)

    def read(self, flow_key, now=0.0):
        flow = self.flows.get(flow_key)
        if flow is None:
            raise ValueError("unknown flow %r" % (flow_key,))
        self._pump(flow, now)
        return {
            "data": bytes(flow.data),
            "gaps": list(flow.gaps),
            "policy": OVERLAP_POLICY,
        }

    def flow_info(self, flow_key):
        flow = self.flows.get(flow_key)
        if flow is None:
            raise ValueError("unknown flow %r" % (flow_key,))
        return {
            "fin_offset": flow.fin_offset,
            "urgent": flow.urgent,
            "buffered": flow.unique_bytes,
            "next_seq": (flow.base_seq + flow.next_rel) & (MOD - 1),
        }

    def stats(self):
        return {
            "flows": len(self.flows),
            "segments": sum(flow.segments for flow in self.flows.values()),
            "bytes": sum(flow.bytes for flow in self.flows.values()),
            "unique_bytes": sum(
                flow.unique_bytes for flow in self.flows.values()
            ),
        }

    def evict(self, now, max_idle=60.0):
        evicted = []
        for flow_key in sorted(self.flows):
            flow = self.flows[flow_key]
            if now - flow.last_arrival >= max_idle:
                evicted.append(flow_key)
        for flow_key in evicted:
            del self.flows[flow_key]
        return evicted

    # ------------------------------------------------------------------
    # Packet parsing: Ethernet / VLAN / QinQ / IPv4 fragmentation / TCP
    # ------------------------------------------------------------------
    def parse_packet(self, raw):
        if len(raw) < 14:
            return None
        offset = 12
        ethertype = struct.unpack_from("!H", raw, offset)[0]
        vlan_tags = []
        offset += 2
        while ethertype in (ETHERTYPE_VLAN, ETHERTYPE_QINQ):
            if offset + 4 > len(raw):
                return None
            tci, ethertype = struct.unpack_from("!HH", raw, offset)
            vlan_tags.append(tci)
            offset += 4
        if ethertype != ETHERTYPE_IPV4:
            return None

        ip_payload = self._ip_payload(raw, offset)
        if ip_payload is None:
            return None
        return self._parse_tcp(ip_payload, vlan_tags)

    def _ip_payload(self, raw, offset):
        if offset + 20 > len(raw):
            return None
        version_ihl, tos, total_length, ident, flags_frag, ttl, protocol, \
            checksum, src, dst = struct.unpack_from(
                "!BBHHHBBH4s4s", raw, offset
            )
        if version_ihl >> 4 != 4:
            return None
        ihl = (version_ihl & 0x0F) * 4
        if ihl < 20 or offset + ihl > len(raw):
            return None
        end = offset + total_length if total_length else len(raw)
        end = min(end, len(raw))
        payload = raw[offset + ihl:end]

        more_fragments = bool(flags_frag & 0x2000)
        frag_offset = (flags_frag & 0x1FFF) * 8
        if not more_fragments and frag_offset == 0:
            return payload

        key = (src, dst, ident, protocol)
        bundle = self._fragments.setdefault(
            key, {"pieces": {}, "total": None, "has_first": False}
        )
        bundle["pieces"][frag_offset] = payload
        if frag_offset == 0:
            bundle["has_first"] = True
        if not more_fragments:
            bundle["total"] = frag_offset + len(payload)

        if bundle["total"] is None or not bundle["has_first"]:
            return None
        expected = 0
        reassembled = bytearray()
        for piece_offset in sorted(bundle["pieces"]):
            if piece_offset != expected:
                return None
            piece = bundle["pieces"][piece_offset]
            reassembled.extend(piece)
            expected += len(piece)
        if expected != bundle["total"]:
            return None
        del self._fragments[key]
        return bytes(reassembled)

    @staticmethod
    def _parse_tcp(ip_payload, vlan_tags):
        if len(ip_payload) < 20:
            return None
        sport, dport, seq, ack, offset_reserved, flags, window, \
            checksum, urgent = struct.unpack_from(
                "!HHIIBBHHH", ip_payload, 0
            )
        data_offset = (offset_reserved >> 4) * 4
        if data_offset < 20 or data_offset > len(ip_payload):
            return None
        return {
            "sport": sport,
            "dport": dport,
            "seq": seq,
            "ack": ack,
            "flags": flags,
            "urgent": urgent if flags & TCP_URG else 0,
            "payload": ip_payload[data_offset:],
            "vlan_tags": vlan_tags,
        }
