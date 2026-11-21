"""Content-addressed build cache keyed by every build input."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


class CacheConflictError(RuntimeError):
    """Raised when two writers store different content under one key."""


class CorruptCacheEntry(ValueError):
    """Raised when stored bytes no longer match their recorded digest."""


class CachePathError(ValueError):
    """Raised when a cache path would escape its namespace."""


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(path: Path) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(os.path.abspath(path), threading.Lock())


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _require_non_empty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL characters")
    return value


def _safe_segment(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise CachePathError(f"{name} must be a non-empty path segment")
    if value in (".", ".."):
        raise CachePathError(f"{name} must not be a relative directory marker")
    if "/" in value or "\\" in value or "\x00" in value:
        raise CachePathError(f"{name} must be a single path segment")
    return value


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

    _META_SUFFIX = ".json"
    _DATA_SUFFIX = ".bin"
    _LOCK_SUFFIX = ".lock"

    def __init__(self, root, tenant, project, clock=None):
        self.root = Path(os.fspath(root))
        self.tenant = _safe_segment("tenant", tenant)
        self.project = _safe_segment("project", project)
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self.clock: Callable[[], float] = clock or time.time

        self._namespace = self.root / self.tenant / self.project
        self._entries = self._namespace / "entries"
        self._revoked = self._namespace / "revoked"

        self._stats_lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._puts = 0
        self._events: list[dict[str, Any]] = []

    def build_key(self, source_digest, branch, lock_digest, toolchain,
                  args=None, platform=None):
        source_digest = _require_non_empty_string("source_digest", source_digest)
        branch = _require_non_empty_string("branch", branch)
        lock_digest = _require_non_empty_string("lock_digest", lock_digest)
        toolchain = _require_non_empty_string("toolchain", toolchain)
        platform = _require_non_empty_string("platform", platform)

        if not isinstance(args, Mapping) or not args:
            raise ValueError("args must be a non-empty mapping")

        normalized_args: dict[str, Any] = {}
        for name, value in args.items():
            if not isinstance(name, str) or not name:
                raise ValueError("argument names must be non-empty strings")
            normalized_args[name] = value
        normalized_args = dict(sorted(normalized_args.items()))

        parts = {
            "source_digest": source_digest,
            "branch": branch,
            "lock_digest": lock_digest,
            "toolchain": toolchain,
            "args": normalized_args,
            "platform": platform,
        }
        try:
            canonical = _canonical_json(parts)
        except (TypeError, ValueError) as exc:
            raise ValueError("key parts must be JSON serializable") from exc
        digest = hashlib.sha256(canonical).hexdigest()
        return {"digest": digest, "parts": parts}

    def put(self, key, content, source="local", expected_digest=None):
        key_digest, parts = self._split_key(key)
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("content must be bytes-like")
        data = bytes(content)
        content_digest = hashlib.sha256(data).hexdigest()
        if expected_digest is not None:
            expected_digest = _require_non_empty_string(
                "expected_digest", expected_digest
            )
        lock_digest = parts.get("lock_digest") if parts else None

        with self._entry_lock(key_digest):
            metadata = self._read_metadata(key_digest, missing_ok=True)
            if metadata is not None:
                stored_digest = metadata.get("digest")
                if not isinstance(stored_digest, str) or not stored_digest:
                    raise CorruptCacheEntry(
                        f"cache entry {key_digest} has invalid metadata"
                    )

                current_matches = self._stored_digest_matches(
                    key_digest, stored_digest
                )
                if stored_digest == content_digest and current_matches:
                    metadata["last_access"] = self._now()
                    self._write_metadata(key_digest, metadata)
                    self._record_put()
                    return None

                if stored_digest != content_digest:
                    if expected_digest is None:
                        raise CacheConflictError(
                            f"cache key {key_digest} already has different content"
                        )
                    if expected_digest != stored_digest:
                        raise CacheConflictError(
                            f"cache key {key_digest} does not match the "
                            "expected digest"
                        )

            now = self._now()
            new_metadata = {
                "key": key_digest,
                "digest": content_digest,
                "size": len(data),
                "parts": parts,
                "lock_digest": lock_digest,
                "created": now,
                "last_access": now,
            }
            metadata_bytes = _canonical_json(new_metadata)

            self._write_bytes_atomic(self._data_path(key_digest), data)
            self._write_bytes_atomic(
                self._metadata_path(key_digest), metadata_bytes
            )
            self._record_put()
        return None

    def get(self, key, source="local"):
        key_digest, parts = self._split_key(key)
        lock_digest = parts.get("lock_digest") if parts else None

        if lock_digest and self._is_revoked(lock_digest):
            self._record_get(key_digest, source, False)
            return None

        with self._entry_lock(key_digest):
            metadata = self._read_metadata(key_digest, missing_ok=True)
            if metadata is None:
                self._record_get(key_digest, source, False)
                return None

            if metadata.get("key") != key_digest:
                self._record_get(key_digest, source, False)
                raise CorruptCacheEntry(
                    f"cache entry {key_digest} has mismatched metadata"
                )

            metadata_lock = metadata.get("lock_digest")
            if (
                isinstance(metadata_lock, str)
                and self._is_revoked(metadata_lock)
            ):
                self._delete_entry(key_digest)
                self._record_get(key_digest, source, False)
                return None

            expected = metadata.get("digest")
            if not isinstance(expected, str) or not expected:
                self._record_get(key_digest, source, False)
                raise CorruptCacheEntry(
                    f"cache entry {key_digest} has invalid metadata"
                )

            try:
                data = self._data_path(key_digest).read_bytes()
            except OSError as exc:
                self._record_get(key_digest, source, False)
                raise CorruptCacheEntry(
                    f"cache entry {key_digest} is missing its content"
                ) from exc

            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                self._record_get(key_digest, source, False)
                raise CorruptCacheEntry(
                    f"cache entry {key_digest} failed its digest check"
                )

            metadata["last_access"] = self._now()
            self._write_metadata(key_digest, metadata)
            self._record_get(key_digest, source, True)
            return {
                "content": data,
                "digest": actual,
                "key": key_digest,
                "source": source,
                "hit": True,
            }

    def resolve_path(self, relative):
        try:
            raw = os.fspath(relative)
        except TypeError as exc:
            raise CachePathError("path must be a string or path-like object") from exc
        if not isinstance(raw, str):
            raise CachePathError("path must be text")
        if not raw or "\x00" in raw:
            raise CachePathError("path must be a non-empty path")

        windows_path = PureWindowsPath(raw)
        candidate = raw.replace("\\", "/")
        windows_candidate = PureWindowsPath(candidate)
        if (
            candidate.startswith("/")
            or windows_path.is_absolute()
            or windows_path.drive
            or windows_candidate.is_absolute()
            or windows_candidate.drive
        ):
            raise CachePathError("absolute paths are not allowed")

        normalized = posixpath.normpath(candidate)
        if normalized == ".." or normalized.startswith("../"):
            raise CachePathError("path escapes the cache namespace")
        return normalized

    def revoke_dependency(self, lock_digest):
        lock_digest = _require_non_empty_string("lock_digest", lock_digest)
        self._ensure_namespace()
        tombstone = self._revoked / (
            hashlib.sha256(lock_digest.encode("utf-8")).hexdigest() + ".json"
        )
        self._write_bytes_atomic(
            tombstone,
            _canonical_json(
                {
                    "lock_digest": lock_digest,
                    "revoked_at": self._now(),
                }
            ),
        )

        removed = 0
        for metadata_path in list(self._entries.glob("*" + self._META_SUFFIX)):
            key_digest = metadata_path.stem
            try:
                self._split_digest(key_digest)
            except CachePathError:
                continue
            with self._entry_lock(key_digest):
                try:
                    metadata = self._read_metadata(key_digest, missing_ok=True)
                except CorruptCacheEntry:
                    metadata = None
                if (
                    metadata is not None
                    and metadata.get("lock_digest") == lock_digest
                ):
                    self._delete_entry(key_digest)
                    removed += 1
        return removed

    def purge(self, ttl, now=None):
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl < 0:
            raise ValueError("ttl must be a non-negative number")
        cutoff = self._now() if now is None else now

        if not self._entries.is_dir():
            return 0

        removed = 0
        for metadata_path in list(self._entries.glob("*" + self._META_SUFFIX)):
            key_digest = metadata_path.stem
            try:
                self._split_digest(key_digest)
            except CachePathError:
                continue
            with self._entry_lock(key_digest):
                try:
                    metadata = self._read_metadata(key_digest, missing_ok=True)
                except CorruptCacheEntry:
                    metadata = None
                if metadata is None:
                    self._delete_entry(key_digest)
                    removed += 1
                    continue

                last_access = metadata.get("last_access")
                if not isinstance(last_access, (int, float)):
                    last_access = metadata.get("created", 0)
                if isinstance(last_access, (int, float)) and cutoff - last_access > ttl:
                    self._delete_entry(key_digest)
                    removed += 1
        return removed

    def stats(self):
        with self._stats_lock:
            counters = {
                "hits": self._hits,
                "misses": self._misses,
                "puts": self._puts,
                "entries": self._entry_count(),
                "events": [dict(event) for event in self._events],
            }
        return counters

    def _split_key(self, key):
        if isinstance(key, str):
            return self._split_digest(key), None
        if not isinstance(key, Mapping):
            raise TypeError("key must be a digest string or a build_key result")
        digest = key.get("digest")
        digest = self._split_digest(digest)
        parts = key.get("parts")
        if parts is not None and not isinstance(parts, Mapping):
            raise ValueError("key parts must be a mapping")
        return digest, parts

    def _split_digest(self, digest):
        if not isinstance(digest, str) or not digest:
            raise ValueError("key digest must be a non-empty string")
        if (
            digest in (".", "..")
            or "/" in digest
            or "\\" in digest
            or "\x00" in digest
        ):
            raise CachePathError("key digest must be a single path segment")
        return digest

    def _now(self):
        return self.clock()

    def _ensure_namespace(self):
        self._entries.mkdir(parents=True, exist_ok=True)
        self._revoked.mkdir(parents=True, exist_ok=True)

    def _data_path(self, key_digest):
        return self._entries / (key_digest + self._DATA_SUFFIX)

    def _metadata_path(self, key_digest):
        return self._entries / (key_digest + self._META_SUFFIX)

    @contextmanager
    def _entry_lock(self, key_digest):
        self._ensure_namespace()
        lock_path = self._entries / (key_digest + self._LOCK_SUFFIX)
        thread_lock = _thread_lock_for(lock_path)
        with thread_lock:
            descriptor = os.open(
                lock_path, os.O_CREAT | os.O_RDWR, 0o600
            )
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _read_metadata(self, key_digest, missing_ok=False):
        path = self._metadata_path(key_digest)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise CorruptCacheEntry(
                f"cache entry {key_digest} has no metadata"
            ) from None
        except OSError as exc:
            raise CorruptCacheEntry(
                f"cache entry {key_digest} metadata cannot be read"
            ) from exc

        try:
            metadata = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptCacheEntry(
                f"cache entry {key_digest} has invalid metadata"
            ) from exc
        if not isinstance(metadata, dict):
            raise CorruptCacheEntry(
                f"cache entry {key_digest} has invalid metadata"
            )
        return metadata

    def _write_metadata(self, key_digest, metadata):
        self._write_bytes_atomic(
            self._metadata_path(key_digest), _canonical_json(metadata)
        )

    def _write_bytes_atomic(self, destination, data):
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix="." + destination.name + ".",
            suffix=".tmp",
            dir=str(destination.parent),
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, destination)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def _stored_digest_matches(self, key_digest, stored_digest):
        try:
            data = self._data_path(key_digest).read_bytes()
        except OSError:
            return False
        return hashlib.sha256(data).hexdigest() == stored_digest

    def _delete_entry(self, key_digest):
        for path in (
            self._metadata_path(key_digest),
            self._data_path(key_digest),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _is_revoked(self, lock_digest):
        if not isinstance(lock_digest, str) or not lock_digest:
            return False
        tombstone = self._revoked / (
            hashlib.sha256(lock_digest.encode("utf-8")).hexdigest() + ".json"
        )
        return tombstone.is_file()

    def _entry_count(self):
        if not self._entries.is_dir():
            return 0
        return sum(
            1 for _ in self._entries.glob("*" + self._META_SUFFIX)
        )

    def _record_put(self):
        with self._stats_lock:
            self._puts += 1

    def _record_get(self, key_digest, source, hit):
        with self._stats_lock:
            if hit:
                self._hits += 1
            else:
                self._misses += 1
            self._events.append(
                {"key": key_digest, "source": source, "hit": hit}
            )
