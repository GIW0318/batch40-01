import math


class FlowRollup:
    _COUNTER_MOD = 1 << 32
    _TCP_FIN = 0x01
    _TCP_RST = 0x04

    def __init__(self, sampling_rate=1, active_timeout=300, idle_timeout=15,
                 bucket_seconds=300, retention=3600):
        self.sampling_rate = sampling_rate
        self.active_timeout = active_timeout
        self.idle_timeout = idle_timeout
        self.bucket_seconds = bucket_seconds
        self.retention = retention
        self._flows = {}
        self._exporter_seq = {}

    @staticmethod
    def _flow_key(record):
        src_ep = (record["src"], record["sport"])
        dst_ep = (record["dst"], record["dport"])
        first, second = (src_ep, dst_ep) if src_ep <= dst_ep else (dst_ep, src_ep)
        direction = 0 if src_ep <= dst_ep else 1
        key = (first, second, record["proto"], record.get("icmp_type"),
               record.get("icmp_code"), record.get("flow_label"))
        return key, direction

    @staticmethod
    def _is_close_flags(flags):
        if flags is None:
            return False
        if isinstance(flags, str):
            text = flags.upper()
            return "FIN" in text or "RST" in text
        if isinstance(flags, bool):
            return False
        if isinstance(flags, int):
            return bool(flags & FlowRollup._TCP_FIN) or bool(flags & FlowRollup._TCP_RST)
        if isinstance(flags, (list, tuple, set)):
            return any(FlowRollup._is_close_flags(item) for item in flags)
        return False

    def _new_flow(self, key, t):
        return {
            "key": key,
            "bytes": 0,
            "packets": 0,
            "counters": [None, None],
            "first_seen": t,
            "last_seen": t,
            "closed": False,
            "buckets": {},
        }

    def _iter_flows(self):
        for group in self._flows.values():
            yield from group

    @staticmethod
    def _public_flow(flow):
        (src, sport), (dst, dport) = flow["key"][0], flow["key"][1]
        return {
            "src": src,
            "sport": sport,
            "dst": dst,
            "dport": dport,
            "proto": flow["key"][2],
            "icmp_type": flow["key"][3],
            "icmp_code": flow["key"][4],
            "flow_label": flow["key"][5],
            "bytes": flow["bytes"],
            "packets": flow["packets"],
            "first_seen": flow["first_seen"],
            "last_seen": flow["last_seen"],
            "closed": flow["closed"],
        }

    def ingest(self, record, now=None):
        exporter = record.get("exporter", "")
        seq = record.get("seq")
        if seq is not None:
            last_seq = self._exporter_seq.get(exporter)
            if last_seq is not None and seq <= last_seq:
                return False
            self._exporter_seq[exporter] = seq

        t = record.get("t", 0) if now is None else now
        key, direction = self._flow_key(record)
        group = self._flows.setdefault(key, [])
        flow = group[-1] if group and not group[-1]["closed"] else None
        if flow is None:
            flow = self._new_flow(key, t)
            group.append(flow)

        raw_bytes = record.get("bytes", 0)
        raw_packets = record.get("packets", 0)
        previous = flow["counters"][direction]
        if previous is None:
            delta_bytes, delta_packets = raw_bytes, raw_packets
        else:
            delta_bytes = (raw_bytes - previous[0]) % self._COUNTER_MOD
            delta_packets = (raw_packets - previous[1]) % self._COUNTER_MOD
        flow["counters"][direction] = (raw_bytes, raw_packets)

        scaled_bytes = delta_bytes * self.sampling_rate
        scaled_packets = delta_packets * self.sampling_rate
        flow["bytes"] += scaled_bytes
        flow["packets"] += scaled_packets
        bucket = (t // self.bucket_seconds) * self.bucket_seconds
        slot = flow["buckets"].setdefault(bucket, [0, 0])
        slot[0] += scaled_bytes
        slot[1] += scaled_packets
        flow["first_seen"] = min(flow["first_seen"], t)
        flow["last_seen"] = max(flow["last_seen"], t)

        if self._is_close_flags(record.get("flags")):
            flow["closed"] = True
        return True

    def flows(self):
        return [self._public_flow(flow) for flow in self._iter_flows()]

    def expire(self, now):
        expired = []
        for flow in self._iter_flows():
            if flow["closed"]:
                continue
            idle = now - flow["last_seen"] > self.idle_timeout
            active = now - flow["first_seen"] > self.active_timeout
            if idle or active:
                flow["closed"] = True
                expired.append(self._public_flow(flow))
        return expired

    def purge(self, now):
        removed = 0
        for key in list(self._flows):
            kept = []
            for flow in self._flows[key]:
                if flow["closed"] and now - flow["last_seen"] > self.retention:
                    removed += 1
                else:
                    kept.append(flow)
            if kept:
                self._flows[key] = kept
            else:
                del self._flows[key]
        return removed

    def rollup(self, bucket_start):
        bucket = (bucket_start // self.bucket_seconds) * self.bucket_seconds
        bytes_total = 0
        packets_total = 0
        contributors = 0
        for flow in self._iter_flows():
            slot = flow["buckets"].get(bucket)
            if slot:
                bytes_total += slot[0]
                packets_total += slot[1]
                contributors += 1
        return {
            "bucket_start": bucket,
            "bytes": bytes_total,
            "packets": packets_total,
            "flows": contributors,
        }

    def top(self, n):
        if self.sampling_rate <= 1:
            margin = 0.0
        else:
            margin = 100.0 / math.sqrt(self.sampling_rate)
        ranked = sorted(self._iter_flows(),
                        key=lambda flow: flow["bytes"], reverse=True)[:n]
        return [dict(self._public_flow(flow), error_margin_pct=margin)
                for flow in ranked]

    def snapshot(self):
        all_flows = list(self._iter_flows())
        active = sum(1 for flow in all_flows if not flow["closed"])
        return {
            "active": active,
            "closed": len(all_flows) - active,
            "flows": len(all_flows),
            "bytes": sum(flow["bytes"] for flow in all_flows),
            "packets": sum(flow["packets"] for flow in all_flows),
        }
