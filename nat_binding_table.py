import heapq
import ipaddress
import itertools
import threading


class NatBindingTable:
    """Outbound NAT binding table with per-protocol timeouts and snapshots."""

    PORT_MIN = 1024
    PORT_MAX = 65535

    def __init__(self, external_ip="198.51.100.1", capacity=1024,
                 tcp_timeout=7200, udp_timeout=60, time_wait=30):
        self.external_ip = external_ip
        self.capacity = capacity
        self.tcp_timeout = tcp_timeout
        self.udp_timeout = udp_timeout
        self.time_wait = time_wait
        self._lock = threading.RLock()
        self._bindings = {}
        self._time_wait = {}
        self._inbound = {}
        self._used_ports = set()
        self._next_port = self.PORT_MIN
        self._expiry_heap = []
        self._seq = itertools.count()
        self._fragments = {}

    def _timeout_for(self, protocol):
        return self.tcp_timeout if protocol == "tcp" else self.udp_timeout

    def _push_expiry(self, kind, key, expires_at):
        heapq.heappush(self._expiry_heap,
                       (expires_at, next(self._seq), kind, key))

    def _refresh(self, binding, now):
        if now < binding["last_seen"]:
            return
        binding["last_seen"] = now
        binding["expires_at"] = now + self._timeout_for(binding["protocol"])
        kind = "time_wait" if binding["state"] == "time_wait" else "active"
        key = (binding["internal_ip"], binding["internal_port"],
               binding["protocol"])
        self._push_expiry(kind, key, binding["expires_at"])

    def _reserve_port(self):
        span = self.PORT_MAX - self.PORT_MIN + 1
        for _ in range(span):
            port = self._next_port
            self._next_port += 1
            if self._next_port > self.PORT_MAX:
                self._next_port = self.PORT_MIN
            if port not in self._used_ports:
                self._used_ports.add(port)
                return port
        raise RuntimeError("no external ports available")

    def _drop_binding(self, binding):
        key = (binding["internal_ip"], binding["internal_port"],
               binding["protocol"])
        self._bindings.pop(key, None)
        self._inbound.pop((binding["protocol"], binding["external_port"]), None)
        self._used_ports.discard(binding["external_port"])

    def _evict_idle(self):
        if not self._bindings:
            return
        victim_key = min(self._bindings,
                         key=lambda k: self._bindings[k]["last_seen"])
        self._drop_binding(self._bindings[victim_key])

    def _ensure_binding(self, internal_ip, internal_port, protocol, now):
        key = (internal_ip, internal_port, protocol)
        binding = self._bindings.get(key)
        if binding is not None:
            self._refresh(binding, now)
            return binding
        if len(self._bindings) >= self.capacity:
            self._evict_idle()
        external_port = self._reserve_port()
        binding = {
            "internal_ip": internal_ip,
            "internal_port": internal_port,
            "protocol": protocol,
            "external_ip": self.external_ip,
            "external_port": external_port,
            "created_at": now,
            "last_seen": now,
            "expires_at": now + self._timeout_for(protocol),
            "state": "active",
        }
        self._bindings[key] = binding
        self._inbound[(protocol, external_port)] = key
        self._push_expiry("active", key, binding["expires_at"])
        return binding

    def allocate(self, internal_ip, internal_port, protocol="tcp", now=0):
        with self._lock:
            return dict(self._ensure_binding(internal_ip, internal_port,
                                             protocol, now))

    def lookup_outbound(self, internal_ip, internal_port, protocol="tcp", now=0):
        with self._lock:
            binding = self._bindings.get((internal_ip, internal_port, protocol))
            if binding is None:
                return None
            self._refresh(binding, now)
            return dict(binding)

    def lookup_inbound(self, remote_ip, remote_port, external_port,
                       protocol="tcp", now=0):
        with self._lock:
            key = self._inbound.get((protocol, external_port))
            binding = self._bindings.get(key) if key is not None else None
            if binding is None:
                return None
            self._refresh(binding, now)
            return dict(binding)

    def _rewrite_source(self, out, protocol, now):
        binding = self._ensure_binding(out["src"], out["sport"], protocol, now)
        out["src"] = self.external_ip
        out["sport"] = binding["external_port"]

    def translate_outbound(self, packet, now=0):
        with self._lock:
            out = dict(packet)
            protocol = packet.get("protocol", "tcp")
            if packet.get("dst") == self.external_ip:
                peer_key = self._inbound.get((protocol, packet.get("dport")))
                peer = self._bindings.get(peer_key) if peer_key else None
                if peer is not None:
                    self._rewrite_source(out, protocol, now)
                    out["dst"] = peer["internal_ip"]
                    out["dport"] = peer["internal_port"]
                    out["hairpin"] = True
                    return out
            self._rewrite_source(out, protocol, now)
            return out

    def translate_inbound(self, packet, now=0):
        with self._lock:
            protocol = packet.get("protocol", "tcp")
            key = self._inbound.get((protocol, packet.get("dport")))
            binding = self._bindings.get(key) if key is not None else None
            if binding is None:
                return dict(packet)
            self._refresh(binding, now)
            out = dict(packet)
            out["dst"] = binding["internal_ip"]
            out["dport"] = binding["internal_port"]
            return out

    def translate_datagram(self, fragments, now=0):
        with self._lock:
            fragments = list(fragments)
            if not fragments:
                return []
            head = fragments[0]
            cache_key = (head.get("src"), head.get("dst"),
                         head.get("protocol"), head.get("id"))
            first = next((f for f in fragments if f.get("offset") == 0), None)
            if first is None:
                cached = self._fragments.setdefault(cache_key, {})
                for fragment in fragments:
                    cached[fragment.get("offset")] = fragment
                return []
            merged = self._fragments.pop(cache_key, {})
            for fragment in fragments:
                merged[fragment.get("offset")] = fragment
            protocol = first.get("protocol", "tcp")
            binding = self._ensure_binding(first["src"], first["sport"],
                                           protocol, now)
            out = []
            for offset in sorted(merged):
                fragment = dict(merged[offset])
                fragment["src"] = self.external_ip
                fragment["sport"] = binding["external_port"]
                if fragment.get("dport") is None:
                    fragment["dport"] = first.get("dport")
                out.append(fragment)
            return out

    def translate_icmp_error(self, icmp, now=0):
        with self._lock:
            inner = icmp.get("inner") or {}
            key = (inner.get("src"), inner.get("sport"),
                   inner.get("protocol", "tcp"))
            binding = self._bindings.get(key)
            result = dict(icmp)
            if binding is None:
                result["matched"] = False
                return result
            self._refresh(binding, now)
            result["matched"] = True
            result["dst"] = binding["internal_ip"]
            result["dport"] = binding["internal_port"]
            return result

    def release(self, internal_ip, internal_port, protocol="tcp", now=0):
        with self._lock:
            key = (internal_ip, internal_port, protocol)
            binding = self._bindings.pop(key, None)
            if binding is None:
                return None
            self._inbound.pop((protocol, binding["external_port"]), None)
            stale = self._time_wait.pop(key, None)
            if stale is not None:
                self._used_ports.discard(stale["external_port"])
            binding["state"] = "time_wait"
            binding["released_at"] = now
            binding["last_seen"] = now
            binding["expires_at"] = now + self.time_wait
            self._time_wait[key] = binding
            self._push_expiry("time_wait", key, binding["expires_at"])
            return dict(binding)

    def expire(self, now=0):
        expired = []
        scanned = 0
        with self._lock:
            while self._expiry_heap and self._expiry_heap[0][0] <= now:
                expires_at, _, kind, key = heapq.heappop(self._expiry_heap)
                table = self._bindings if kind == "active" else self._time_wait
                binding = table.get(key)
                if binding is None or binding["expires_at"] != expires_at:
                    continue
                scanned += 1
                del table[key]
                self._inbound.pop(
                    (binding["protocol"], binding["external_port"]), None)
                self._used_ports.discard(binding["external_port"])
                expired.append(dict(binding))
        return {"expired": expired, "scanned": scanned}

    def map_ipv6(self, address, internal_prefix, external_prefix, reverse=False):
        addr = ipaddress.IPv6Address(address)
        src_net = ipaddress.IPv6Network(
            external_prefix if reverse else internal_prefix)
        dst_net = ipaddress.IPv6Network(
            internal_prefix if reverse else external_prefix)
        host = int(addr) - int(src_net.network_address)
        if host < 0 or host >= (1 << (128 - dst_net.prefixlen)):
            raise ValueError("address does not fit target prefix")
        return str(ipaddress.IPv6Address(int(dst_net.network_address) + host))

    def snapshot(self):
        with self._lock:
            return ([dict(b) for b in self._bindings.values()]
                    + [dict(b) for b in self._time_wait.values()])

    def stats(self):
        with self._lock:
            return {
                "bindings": len(self._bindings) + len(self._time_wait),
                "active": len(self._bindings),
                "time_wait": len(self._time_wait),
                "capacity": self.capacity,
                "ports_in_use": len(self._used_ports),
            }
