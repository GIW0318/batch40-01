"""Connection tracking state synchronization for active/standby failover."""


class ConntrackSyncAgent:
    """Keeps a conntrack table that can be exported, applied and reconciled."""

    _ENTRY_FIELDS = (
        "proto",
        "src",
        "dst",
        "state",
        "seq_local",
        "seq_remote",
        "window",
        "window_scale",
        "nat_src",
        "nat_dst",
        "timeout",
        "priority",
        "related",
    )

    def __init__(self, role="primary", timeout=120, table_limit=1000):
        self.role = role
        self.timeout = int(timeout)
        self.table_limit = max(1, int(table_limit))
        self._entries = {}
        self._nat_index = {}
        self._version = 0
        self._in_sync = True
        self._evicted_capacity = 0
        self._evicted_timeout = 0

    @staticmethod
    def _tuple(value):
        if value is None:
            return None
        return tuple(value)

    @classmethod
    def _key(cls, proto, src, dst):
        src = cls._tuple(src)
        dst = cls._tuple(dst)
        return (proto, src[0], src[1], dst[0], dst[1])

    def _entry_key(self, entry):
        key = entry.get("key")
        if key is not None:
            return tuple(key)
        return self._key(entry["proto"], entry["src"], entry["dst"])

    def _normalize_entry(self, entry, now):
        normalized = {}
        for field in self._ENTRY_FIELDS:
            if field in entry:
                normalized[field] = entry[field]

        src = self._tuple(normalized.get("src"))
        dst = self._tuple(normalized.get("dst"))
        if src is None or dst is None:
            raise ValueError("connection entry requires src and dst tuples")

        normalized["src"] = src
        normalized["dst"] = dst
        normalized["nat_src"] = self._tuple(normalized.get("nat_src"))
        normalized["nat_dst"] = self._tuple(normalized.get("nat_dst"))
        normalized["related"] = self._tuple(normalized.get("related"))
        normalized["window_scale"] = int(normalized.get("window_scale", 0) or 0)
        normalized["window"] = int(normalized.get("window", 0) or 0)
        normalized["priority"] = int(normalized.get("priority", 0) or 0)
        normalized["timeout"] = max(
            int(normalized.get("timeout", self.timeout) or 0), self.timeout
        )
        normalized["last_seen"] = entry.get("last_seen", now)
        normalized["version"] = int(entry.get("version", 0) or 0)
        normalized["key"] = self._entry_key(dict(entry, src=src, dst=dst))
        return normalized

    def _nat_keys(self, entry):
        src = entry["src"]
        dst = entry["dst"]
        nat_src = entry.get("nat_src") or src
        nat_dst = entry.get("nat_dst") or dst
        if nat_src == src and nat_dst == dst:
            return ()
        return (
            self._key(entry["proto"], nat_src, nat_dst),
            self._key(entry["proto"], nat_dst, nat_src),
        )

    def _index_nat(self, entry):
        for nat_key in self._nat_keys(entry):
            self._nat_index[nat_key] = entry["key"]

    def _unindex_nat(self, entry):
        for nat_key in self._nat_keys(entry):
            if self._nat_index.get(nat_key) == entry["key"]:
                del self._nat_index[nat_key]

    def _remove(self, key):
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._unindex_nat(entry)
        return entry

    def _store(self, entry):
        old = self._entries.get(entry["key"])
        if old is not None:
            self._unindex_nat(old)
        self._entries[entry["key"]] = entry
        self._index_nat(entry)
        self._version = max(self._version, entry["version"])

    @staticmethod
    def _expired(entry, now):
        return now - entry.get("last_seen", now) >= entry["timeout"]

    def _purge_expired(self, now):
        expired = [
            key for key, entry in self._entries.items() if self._expired(entry, now)
        ]
        for key in expired:
            self._remove(key)
        self._evicted_timeout += len(expired)
        return len(expired)

    def _capacity_admit(self, entry):
        if entry["key"] in self._entries:
            return True
        if len(self._entries) < self.table_limit:
            return True

        victim_key = min(
            self._entries,
            key=lambda key: (
                self._entries[key]["priority"],
                self._entries[key].get("last_seen", 0),
                self._entries[key].get("version", 0),
            ),
        )
        victim = self._entries[victim_key]
        if entry["priority"] <= victim["priority"]:
            return False

        self._remove(victim_key)
        self._evicted_capacity += 1
        return True

    def _copy_entry(self, entry):
        copied = dict(entry)
        copied["key"] = tuple(entry["key"])
        return copied

    def track(self, flow, now=0):
        version = self._version + 1
        entry = dict(flow)
        entry["version"] = version
        entry["last_seen"] = now
        entry = self._normalize_entry(entry, now)

        existing = self._entries.get(entry["key"])
        if existing is None and not self._capacity_admit(entry):
            return None

        self._version = version
        self._store(entry)
        return self._copy_entry(entry)

    def _resolve(self, key, now):
        entry = self._entries.get(key)
        if entry is None or self._expired(entry, now):
            return None
        return entry

    def lookup(self, proto, src, dst, now=0):
        self._purge_expired(now)
        key = self._key(proto, src, dst)

        entry = self._resolve(key, now)
        matched = "exact"
        if entry is None:
            nat_key = self._nat_index.get(key)
            if nat_key is not None:
                entry = self._resolve(nat_key, now)
                matched = "nat"

        if entry is None:
            reverse_key = self._key(proto, dst, src)
            entry = self._resolve(reverse_key, now)
            matched = "reverse"

        if entry is None:
            return None

        if entry.get("related") is not None:
            related = self._resolve(tuple(entry["related"]), now)
            if related is not None:
                result = self._copy_entry(related)
                result["matched"] = "related"
                result["via_related"] = True
                return result

        result = self._copy_entry(entry)
        result["matched"] = matched
        result["via_related"] = False
        return result

    def _find_for_seq(self, proto, src, dst, now):
        connected = self.lookup(proto, src, dst, now=now)
        if connected is None:
            return None
        return self._entries.get(tuple(connected["key"]))

    def check_seq(self, proto, src, dst, direction, seq, now=0):
        entry = self._find_for_seq(proto, src, dst, now)
        if entry is None:
            return {
                "translated": None,
                "in_window": False,
                "direction": direction,
                "found": False,
            }

        if direction in ("out", "outbound", "local"):
            translated = seq + (entry["seq_local"] - entry["seq_remote"])
            window_start = entry["seq_local"]
        else:
            translated = seq + (entry["seq_remote"] - entry["seq_local"])
            window_start = entry["seq_remote"]

        in_window = window_start <= translated < window_start + entry["window"]
        return {
            "translated": translated,
            "in_window": in_window,
            "direction": direction,
            "found": True,
        }

    def effective_window(self, proto, src, dst):
        key = self._key(proto, src, dst)
        entry = self._entries.get(key)
        if entry is None:
            nat_key = self._nat_index.get(key)
            if nat_key is not None:
                entry = self._entries.get(nat_key)
        if entry is None:
            entry = self._entries.get(self._key(proto, dst, src))
        if entry is None:
            return None
        return entry["window"] << entry["window_scale"]

    def export_full(self):
        return [self._copy_entry(entry) for entry in self._entries.values()]

    def export_incremental(self, since=0):
        entries = [
            self._copy_entry(entry)
            for entry in self._entries.values()
            if entry["version"] > since
        ]
        return sorted(entries, key=lambda entry: entry["version"])

    def apply(self, entries, now=0):
        applied = 0
        ignored = 0
        for incoming in entries:
            entry = self._normalize_entry(incoming, now)
            current = self._entries.get(entry["key"])
            if current is not None and entry["version"] <= current["version"]:
                ignored += 1
                continue
            if current is None and not self._capacity_admit(entry):
                ignored += 1
                continue
            self._store(entry)
            applied += 1
        return {
            "applied": applied,
            "ignored": ignored,
            "count": len(self._entries),
        }

    def tick(self, now):
        return self._purge_expired(now)

    def link_down(self, now=0):
        self._in_sync = False
        self._link_down_at = now

    def resync(self, peer_entries, now=0):
        peer_map = {}
        for incoming in peer_entries:
            entry = self._normalize_entry(incoming, now)
            current = peer_map.get(entry["key"])
            if current is None or entry["version"] > current["version"]:
                peer_map[entry["key"]] = entry

        old_keys = set(self._entries)
        removed = len(old_keys - set(peer_map))

        for key in list(self._entries):
            self._remove(key)
        self._nat_index.clear()

        rebuilt = 0
        for entry in peer_map.values():
            if self._capacity_admit(entry):
                self._store(entry)
                rebuilt += 1

        self._in_sync = True
        return {
            "rebuilt": rebuilt,
            "removed": removed,
            "in_sync": self._in_sync,
        }

    def diff(self, peer_entries):
        local = {entry["key"]: entry for entry in self._entries.values()}
        peer = {}
        for incoming in peer_entries:
            key = self._entry_key(incoming)
            entry = dict(incoming)
            entry.setdefault("key", key)
            entry["version"] = int(entry.get("version", 0) or 0)
            peer[key] = entry

        local_keys = set(local)
        peer_keys = set(peer)
        missing = sorted(local_keys - peer_keys)
        extra = sorted(peer_keys - local_keys)
        mismatch = sorted(
            key
            for key in local_keys & peer_keys
            if local[key]["version"] != peer[key]["version"]
        )
        return {
            "missing_on_peer": [list(key) for key in missing],
            "extra_on_peer": [list(key) for key in extra],
            "version_mismatch": [list(key) for key in mismatch],
        }

    def snapshot(self):
        return {
            "role": self.role,
            "count": len(self._entries),
            "in_sync": self._in_sync,
            "version": self._version,
            "evicted_capacity": self._evicted_capacity,
            "evicted_timeout": self._evicted_timeout,
        }
