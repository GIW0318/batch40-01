"""Content-addressed build cache keyed by every build input."""


class CacheConflictError(RuntimeError):
    """Raised when two writers store different content under one key."""


class CorruptCacheEntry(ValueError):
    """Raised when stored bytes no longer match their recorded digest."""


class CachePathError(ValueError):
    """Raised when a cache path would escape its namespace."""


class ArtifactCacheKey:
    """Build cache with strict keys, integrity checks and isolation.

    ``root`` is the cache root, ``tenant``/``project`` select an isolated
    namespace below it and ``clock`` returns epoch seconds.

    ``build_key(source_digest, branch, lock_digest, toolchain, args=None,
    platform=None)`` returns ``{"digest", "parts"}``.  All inputs are
    mandatory and non-empty; ``args`` is a mapping of option name to value
    recorded sorted by name, so argument order never changes the key.  The
    digest is the sha256 of the canonical JSON of the parts.

    ``put(key, content, source="local", expected_digest=None)`` stages the
    entry in a temporary file and atomically replaces the final file, storing
    the content sha256.  Different content under an existing key raises
    ``CacheConflictError`` unless ``expected_digest`` equals the stored
    digest; identical content is idempotent.

    ``get(key, source="local")`` verifies the stored digest and raises
    ``CorruptCacheEntry`` on a mismatch.  It returns ``{"content", "digest",
    "key", "source", "hit"}`` or None on a miss.  Entries are only visible
    under the same tenant and project.

    ``resolve_path(relative)`` normalizes ``.``/``..`` and returns a path
    relative to the namespace; absolute paths and escapes raise
    ``CachePathError``.

    ``revoke_dependency(lock_digest)`` invalidates every key built from that
    lock digest.  ``purge(ttl, now=None)`` removes entries whose last access
    is older than ``ttl`` seconds and returns the number removed.  ``stats()``
    returns counters plus an ``events`` list of ``{"key", "source", "hit"}``
    records.
    """

    def __init__(self, root, tenant, project, clock=None):
        self.root = root
        self.tenant = tenant
        self.project = project
        self.clock = clock

    def build_key(self, source_digest, branch, lock_digest, toolchain,
                  args=None, platform=None):
        raise NotImplementedError

    def put(self, key, content, source="local", expected_digest=None):
        raise NotImplementedError

    def get(self, key, source="local"):
        raise NotImplementedError

    def resolve_path(self, relative):
        raise NotImplementedError

    def revoke_dependency(self, lock_digest):
        raise NotImplementedError

    def purge(self, ttl, now=None):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
