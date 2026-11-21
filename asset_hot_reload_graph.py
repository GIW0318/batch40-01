"""Asset hot reload dependency graph with debounce and lease safety."""


class HotReloadGraph:
    def __init__(self, debounce_ms=200, large_asset_mb=50.0):
        self.debounce_ms = debounce_ms
        self.large_asset_mb = large_asset_mb
        self.assets = {}
        self.last_event_ms = {}
        self.versions = {}
        self.leases = {}
        self.variant_cache = {}
        self.mtimes = {}

    def add_asset(self, path, deps=(), size_mb=0.0, kind="model", mtime=0):
        raise NotImplementedError

    def remove_asset(self, path):
        raise NotImplementedError

    def deps(self, path):
        raise NotImplementedError

    def dependents(self, path):
        raise NotImplementedError

    def reload_order(self, changed):
        raise NotImplementedError

    def watch(self, events, now_ms):
        raise NotImplementedError

    def swap(self, path, version, valid=True):
        raise NotImplementedError

    def version(self, path):
        raise NotImplementedError

    def read(self, path):
        raise NotImplementedError

    def lease(self, path):
        raise NotImplementedError

    def release(self, handle):
        raise NotImplementedError

    def variants(self, path):
        raise NotImplementedError

    def cache_variant(self, path, variant):
        raise NotImplementedError

    def invalidate_variants(self, path):
        raise NotImplementedError

    def reload(self, path, size_mb=None):
        raise NotImplementedError

    def poll(self, snapshot):
        raise NotImplementedError
