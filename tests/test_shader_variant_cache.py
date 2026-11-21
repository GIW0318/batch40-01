import pytest

import shader_variant_cache as svc


def test_keyword_order_and_case_are_normalized():
    cache = svc.ShaderVariantCache()
    assert cache.variant_key(["snow", "fog"]) == cache.variant_key(
        ["fog", "Snow"]
    )


def test_constant_order_is_normalized():
    cache = svc.ShaderVariantCache()
    assert cache.variant_key(["fog"], {"a": 1, "b": 2}) == cache.variant_key(
        ["fog"], {"b": 2, "a": 1}
    )


def test_different_constant_values_are_different_variants():
    cache = svc.ShaderVariantCache()
    first = cache.request("mat", ["fog"], {"shadow": 1})
    second = cache.request("mat", ["fog"], {"shadow": 2})
    assert first["key"] != second["key"]
    assert first["status"] == "compiling"
    assert second["status"] == "compiling"
    assert first["token"] != second["token"]


def test_completed_compile_is_a_hit():
    cache = svc.ShaderVariantCache()
    pending = cache.request("mat", ["fog"])
    assert pending["status"] == "compiling"
    assert cache.complete_compile(pending["token"], b"binary") is True
    hit = cache.request("mat", ["fog"])
    assert hit["status"] == "hit"
    assert hit["handle"] is not None


def test_pending_request_reuses_one_token():
    cache = svc.ShaderVariantCache()
    first = cache.request("mat", ["fog"])
    second = cache.request("mat", ["fog"])
    assert first["token"] == second["token"]
    assert cache.cache_info()["compiling"] == 1


def test_driver_change_invalidates_compiled_cache():
    cache = svc.ShaderVariantCache()
    pending = cache.request("mat", ["fog"])
    cache.complete_compile(pending["token"], b"binary")
    assert cache.request("mat", ["fog"])["status"] == "hit"
    cache.set_driver("d2")
    assert cache.request("mat", ["fog"])["status"] == "compiling"


def test_stale_callback_after_driver_change_is_rejected():
    cache = svc.ShaderVariantCache()
    pending = cache.request("mat", ["fog"])
    cache.set_driver("d2")
    assert cache.complete_compile(pending["token"], b"binary") is False
    assert cache.request("mat", ["fog"])["status"] == "compiling"


def test_hot_reload_invalidates_completed_cache():
    cache = svc.ShaderVariantCache()
    pending = cache.request("mat", ["fog"])
    cache.complete_compile(pending["token"], b"binary")
    assert cache.request("mat", ["fog"])["status"] == "hit"
    cache.hot_reload("s2")
    assert cache.request("mat", ["fog"])["status"] == "compiling"
    assert cache.cache_info()["source_version"] == "s2"


def test_material_default_change_uses_a_new_variant():
    cache = svc.ShaderVariantCache()
    cache.set_material_keywords("mat", ["fog"])
    first = cache.request("mat")
    cache.set_material_keywords("mat", ["snow"])
    second = cache.request("mat")
    assert first["key"] != second["key"]
    assert second["status"] == "compiling"


def test_prewarm_respects_frame_budget():
    cache = svc.ShaderVariantCache()
    entries = [
        {"material": "m1", "keywords": ["a"]},
        {"material": "m2", "keywords": ["b"]},
        {"material": "m3", "keywords": ["c"]},
    ]
    result = cache.prewarm(entries, frame_budget=1)
    assert result == {"started": 1, "remaining": 2}


def test_prewarm_resumes_across_frames():
    cache = svc.ShaderVariantCache()
    entries = [
        {"material": "m1", "keywords": ["a"]},
        {"material": "m2", "keywords": ["b"]},
        {"material": "m3", "keywords": ["c"]},
    ]
    cache.prewarm(entries, frame_budget=1)
    second = cache.prewarm(entries, frame_budget=1)
    assert second == {"started": 1, "remaining": 1}
    third = cache.prewarm(entries, frame_budget=1)
    assert third == {"started": 1, "remaining": 0}
    fourth = cache.prewarm(entries, frame_budget=1)
    assert fourth == {"started": 0, "remaining": 0}


def test_evict_skips_in_flight_compiles():
    cache = svc.ShaderVariantCache()
    cache.request("mat", ["fog"])
    assert cache.evict(5) == []


def test_evict_removes_least_recently_requested():
    cache = svc.ShaderVariantCache()
    first = cache.request("mat", ["a"])
    cache.complete_compile(first["token"], b"a")
    second = cache.request("mat", ["b"])
    cache.complete_compile(second["token"], b"b")
    cache.request("mat", ["a"])
    assert cache.evict(1) == [second["key"]]
    assert cache.request("mat", ["b"])["status"] == "compiling"


def test_cache_limit_records_fallback():
    cache = svc.ShaderVariantCache(limit=2)
    first = cache.request("mat", ["a"])
    cache.complete_compile(first["token"], b"a")
    second = cache.request("mat", ["b"])
    cache.complete_compile(second["token"], b"b")
    result = cache.request("mat", ["c"])
    assert result["status"] == "fallback"
    log = cache.fallback_log()
    assert len(log) == 1
    assert log[0]["key"] == result["key"]
    assert log[0]["reason"] == "limit"


def test_cache_info_reports_versions_and_sizes():
    cache = svc.ShaderVariantCache(
        engine_version="eng7", driver_version="drv3", source_version="src9"
    )
    info = cache.cache_info()
    assert info["engine_version"] == "eng7"
    assert info["driver_version"] == "drv3"
    assert info["source_version"] == "src9"
    pending = cache.request("mat", ["a"])
    cache.complete_compile(pending["token"], b"a")
    assert cache.cache_info()["size"] == 1
    cache.request("mat", ["b"])
    assert cache.cache_info()["compiling"] == 1
