"""Shader variant cache.

Contract used by the tests
--------------------------
* ``variant_key(keywords, constants=None)``: keywords are case-insensitive
  strings; the key is the lower-case keywords sorted and joined with ",",
  then "|", then constants sorted as "name=value" joined with ",".  Input
  order never matters.
* ``request(material, keywords=None, constants=None)`` uses the material
  default keywords when keywords is None.  Returns {"key", "status",
  "token", "handle"}:
  - "hit" when a compiled variant exists (handle is set);
  - "compiling" for a new variant (token identifies the pending compile,
    handle is None); repeated requests for the same pending key return the
    same token and start no second compile;
  - "fallback" when the cache is at ``limit`` entries; a record is appended
    to the fallback log and no compile starts.
* ``complete_compile(token, binary)`` returns True and publishes the variant
  for a live token.  It returns False for a token invalidated by set_driver,
  hot_reload or eviction, and such a result never resurrects the slot.
* ``set_material_keywords(material, keywords)`` changes a material default;
  later requests use the new key instead of a stale hit.
* Entries are versioned by engine_version, driver_version and
  source_version.  ``set_driver(version)`` and ``hot_reload(version)``
  invalidate every compiled and pending entry (pending tokens become
  stale).  ``cache_info()`` returns engine_version, driver_version,
  source_version, size and compiling.
* ``prewarm(entries, frame_budget=1)``: entries are dicts with material,
  keywords, constants.  At most frame_budget variants are started per call;
  the cursor persists across calls.  Returns {"started": n, "remaining": m}.
* ``evict(count=1)`` evicts least-recently-requested compiled entries and
  never entries still compiling; returns the evicted keys.  A hit refreshes
  recency.
* ``fallback_log()`` returns {"key", "reason"} records in order.
"""

import threading
from collections import OrderedDict


class ShaderVariantCache:
    def __init__(self, engine_version="e1", driver_version="d1",
                 source_version="s1", limit=64):
        self.engine_version = engine_version
        self.driver_version = driver_version
        self.source_version = source_version
        self.limit = limit
        self._lock = threading.RLock()
        self._compiled = OrderedDict()
        self._pending = {}
        self._tokens = {}
        self._material_keywords = {}
        self._fallback_log = []
        self._generation = 0
        self._token_counter = 0
        self._prewarm_cursor = 0
        self._prewarm_entries_id = None

    def variant_key(self, keywords, constants=None):
        normalized = sorted(
            {str(keyword).lower() for keyword in (keywords or ())}
        )
        keyword_part = ",".join(normalized)
        constant_part = ""
        if constants:
            constant_part = ",".join(
                f"{name}={constants[name]}" for name in sorted(constants)
            )
        return f"{keyword_part}|{constant_part}"

    def set_material_keywords(self, material, keywords):
        with self._lock:
            self._material_keywords[material] = list(keywords or ())

    def request(self, material, keywords=None, constants=None):
        with self._lock:
            if keywords is None:
                keywords = self._material_keywords.get(material, ())
            key = self.variant_key(keywords, constants)
            return self._request_locked(key)

    def complete_compile(self, token, binary):
        with self._lock:
            pending = self._tokens.pop(token, None)
            if pending is None or pending.generation != self._generation:
                return False
            self._pending.pop(pending.key, None)
            self._compiled[pending.key] = _VariantHandle(pending.key, binary)
            return True

    def prewarm(self, entries, frame_budget=1):
        with self._lock:
            entries_id = id(entries)
            if entries_id != self._prewarm_entries_id:
                self._prewarm_entries_id = entries_id
                self._prewarm_cursor = 0
            total = len(entries)
            index = self._prewarm_cursor
            started = 0
            while index < total and started < frame_budget:
                entry = entries[index]
                index += 1
                key = self.variant_key(
                    entry.get("keywords"), entry.get("constants")
                )
                already_known = key in self._compiled or key in self._pending
                result = self._request_locked(key)
                if not already_known and result["status"] == "compiling":
                    started += 1
            self._prewarm_cursor = index
            return {"started": started, "remaining": total - index}

    def evict(self, count=1):
        with self._lock:
            evicted = []
            while count > 0 and self._compiled:
                key = next(iter(self._compiled))
                del self._compiled[key]
                evicted.append(key)
                count -= 1
            return evicted

    def fallback_log(self):
        with self._lock:
            return [dict(record) for record in self._fallback_log]

    def set_driver(self, driver_version):
        with self._lock:
            self.driver_version = driver_version
            self._invalidate()

    def hot_reload(self, source_version):
        with self._lock:
            self.source_version = source_version
            self._invalidate()

    def cache_info(self):
        with self._lock:
            return {
                "engine_version": self.engine_version,
                "driver_version": self.driver_version,
                "source_version": self.source_version,
                "size": len(self._compiled),
                "compiling": len(self._pending),
            }

    def _request_locked(self, key):
        handle = self._compiled.get(key)
        if handle is not None:
            self._compiled.move_to_end(key)
            return {
                "key": key,
                "status": "hit",
                "token": None,
                "handle": handle,
            }
        pending = self._pending.get(key)
        if pending is not None:
            return {
                "key": key,
                "status": "compiling",
                "token": pending.token,
                "handle": None,
            }
        if len(self._compiled) + len(self._pending) >= self.limit:
            self._fallback_log.append({"key": key, "reason": "limit"})
            return {
                "key": key,
                "status": "fallback",
                "token": None,
                "handle": None,
            }
        self._token_counter += 1
        token = f"token-{self._token_counter}"
        pending = _PendingCompile(token, key, self._generation)
        self._pending[key] = pending
        self._tokens[token] = pending
        return {
            "key": key,
            "status": "compiling",
            "token": token,
            "handle": None,
        }

    def _invalidate(self):
        self._generation += 1
        self._compiled.clear()
        self._pending.clear()
        self._tokens.clear()
        self._prewarm_cursor = 0
        self._prewarm_entries_id = None


class _PendingCompile:
    __slots__ = ("token", "key", "generation")

    def __init__(self, token, key, generation):
        self.token = token
        self.key = key
        self.generation = generation


class _VariantHandle:
    __slots__ = ("key", "binary")

    def __init__(self, key, binary):
        self.key = key
        self.binary = binary
