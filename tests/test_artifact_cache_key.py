import hashlib

import pytest

import artifact_cache_key as ack


def build(cache, **overrides):
    parts = {
        "source_digest": "a" * 64,
        "branch": "main",
        "lock_digest": "b" * 64,
        "toolchain": "gcc-13.2.1",
        "args": {"O2": "1", "arch": "x86_64"},
        "platform": "linux-x86_64",
    }
    parts.update(overrides)
    return cache.build_key(**parts)


def cache_for(tmp_path, tenant="tenant-a", project="proj", clock=None):
    return ack.ArtifactCacheKey(tmp_path / "cache", tenant, project, clock=clock)


def test_key_is_deterministic_and_argument_order_independent(tmp_path):
    cache = cache_for(tmp_path)
    first = build(cache, args={"O2": "1", "arch": "x86_64"})
    second = build(cache, args={"arch": "x86_64", "O2": "1"})
    assert first["digest"] == second["digest"]
    assert first["parts"]["args"] == {"O2": "1", "arch": "x86_64"}
    assert len(first["digest"]) == 64


def test_branch_is_part_of_the_key(tmp_path):
    cache = cache_for(tmp_path)
    assert build(cache, branch="feature/x")["digest"] != build(cache)["digest"]


def test_platform_and_toolchain_are_part_of_the_key(tmp_path):
    cache = cache_for(tmp_path)
    base = build(cache)["digest"]
    assert build(cache, platform="linux-arm64")["digest"] != base
    assert build(cache, toolchain="gcc-14.1.0")["digest"] != base


def test_source_and_lock_digests_are_part_of_the_key(tmp_path):
    cache = cache_for(tmp_path)
    base = build(cache)["digest"]
    assert build(cache, source_digest="c" * 64)["digest"] != base
    assert build(cache, lock_digest="d" * 64)["digest"] != base


def test_missing_key_component_is_rejected(tmp_path):
    cache = cache_for(tmp_path)
    with pytest.raises(ValueError):
        build(cache, platform=None)
    with pytest.raises(ValueError):
        build(cache, source_digest="")


def test_put_and_get_round_trip(tmp_path):
    cache = cache_for(tmp_path)
    key = build(cache)
    cache.put(key, b"artifact")
    result = cache.get(key)
    assert result["hit"] is True
    assert result["content"] == b"artifact"
    assert result["digest"] == hashlib.sha256(b"artifact").hexdigest()
    assert result["key"] == key["digest"]


def test_corrupt_entry_is_refused(tmp_path):
    cache = cache_for(tmp_path)
    key = build(cache)
    cache.put(key, b"artifact")
    stored = list((tmp_path / "cache").rglob("*.bin"))
    assert len(stored) == 1
    stored[0].write_bytes(b"junk")
    with pytest.raises(ack.CorruptCacheEntry):
        cache.get(key)


def test_tenant_and_project_namespaces_are_isolated(tmp_path):
    key = build(cache_for(tmp_path, "tenant-a", "proj"))
    cache_for(tmp_path, "tenant-a", "proj").put(key, b"x")
    assert cache_for(tmp_path, "tenant-b", "proj").get(key) is None
    assert cache_for(tmp_path, "tenant-a", "other").get(key) is None
    assert cache_for(tmp_path, "tenant-a", "proj").get(key)["content"] == b"x"


def test_resolve_path_normalizes_and_blocks_escape(tmp_path):
    cache = cache_for(tmp_path)
    assert cache.resolve_path("a/./b") == "a/b"
    with pytest.raises(ack.CachePathError):
        cache.resolve_path("../other")
    with pytest.raises(ack.CachePathError):
        cache.resolve_path("/absolute")


def test_conflicting_content_needs_an_explicit_expectation(tmp_path):
    cache = cache_for(tmp_path)
    key = build(cache)
    cache.put(key, b"first")
    cache.put(key, b"first")
    with pytest.raises(ack.CacheConflictError):
        cache.put(key, b"second")
    old = hashlib.sha256(b"first").hexdigest()
    cache.put(key, b"second", expected_digest=old)
    assert cache.get(key)["content"] == b"second"


def test_purge_uses_last_access_time(tmp_path):
    now = [0]
    cache = cache_for(tmp_path, clock=lambda: now[0])
    fresh = build(cache, source_digest="a" * 64)
    stale = build(cache, source_digest="e" * 64)
    cache.put(fresh, b"fresh")
    cache.put(stale, b"stale")
    now[0] = 50
    assert cache.get(fresh)["content"] == b"fresh"
    now[0] = 100
    assert cache.purge(60) == 1
    assert cache.get(fresh) is not None
    assert cache.get(stale) is None


def test_revoked_dependency_invalidates_related_keys(tmp_path):
    cache = cache_for(tmp_path)
    revoked = build(cache, lock_digest="b" * 64)
    kept = build(cache, lock_digest="c" * 64)
    cache.put(revoked, b"old")
    cache.put(kept, b"keep")
    cache.revoke_dependency("b" * 64)
    assert cache.get(revoked) is None
    assert cache.get(kept)["content"] == b"keep"


def test_stats_record_key_source_and_hit_result(tmp_path):
    cache = cache_for(tmp_path)
    hit = build(cache, source_digest="a" * 64)
    miss = build(cache, source_digest="f" * 64)
    cache.put(hit, b"x")
    cache.get(hit, source="remote")
    cache.get(miss, source="remote")
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["events"][-2:] == [
        {"key": hit["digest"], "source": "remote", "hit": True},
        {"key": miss["digest"], "source": "remote", "hit": False},
    ]


def test_stats_counts_puts_and_entries(tmp_path):
    cache = cache_for(tmp_path)
    cache.put(build(cache), b"x")
    stats = cache.stats()
    assert stats["puts"] == 1
    assert stats["entries"] == 1


def test_put_leaves_no_temporary_files(tmp_path):
    cache = cache_for(tmp_path)
    cache.put(build(cache), b"artifact")
    assert [path for path in (tmp_path / "cache").rglob("*.tmp")] == []
