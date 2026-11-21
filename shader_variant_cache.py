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


class ShaderVariantCache:
    def __init__(self, engine_version="e1", driver_version="d1",
                 source_version="s1", limit=64):
        self.engine_version = engine_version
        self.driver_version = driver_version
        self.source_version = source_version
        self.limit = limit

    def variant_key(self, keywords, constants=None):
        raise NotImplementedError

    def set_material_keywords(self, material, keywords):
        raise NotImplementedError

    def request(self, material, keywords=None, constants=None):
        raise NotImplementedError

    def complete_compile(self, token, binary):
        raise NotImplementedError

    def prewarm(self, entries, frame_budget=1):
        raise NotImplementedError

    def evict(self, count=1):
        raise NotImplementedError

    def fallback_log(self):
        raise NotImplementedError

    def set_driver(self, driver_version):
        raise NotImplementedError

    def hot_reload(self, source_version):
        raise NotImplementedError

    def cache_info(self):
        raise NotImplementedError
