import pytest

import wasm_import_allowlist as wia


ALLOWED = {"env": {"log", "now"}, "wasi_snapshot_preview1": {"fd_write"}}


def module(**overrides):
    base = {
        "name": "plugin",
        "imports": [{"module": "env", "field": "log"}],
        "memories": [{"initial": 1, "max": 4}],
        "tables": [{"entries": ["i32->i32"]}],
        "start_pages": 0,
    }
    base.update(overrides)
    return base


def codes(result):
    return [issue["code"] for issue in result["issues"]]


def test_allowed_module_and_field_pass_validation():
    result = wia.WasmImportAllowlist(ALLOWED).validate_module(module())
    assert result["ok"] is True
    assert result["issues"] == []


def test_denied_field_of_an_allowed_module_is_reported():
    result = wia.WasmImportAllowlist(ALLOWED).validate_module(
        module(imports=[{"module": "env", "field": "exec"}])
    )
    assert result["ok"] is False
    assert "import_denied" in codes(result)


def test_denied_module_is_reported():
    result = wia.WasmImportAllowlist(ALLOWED).validate_module(
        module(imports=[{"module": "fs", "field": "read"}])
    )
    assert result["ok"] is False
    assert "import_denied" in codes(result)


def test_dependency_imports_are_checked_recursively():
    result = wia.WasmImportAllowlist(ALLOWED).validate_module(
        module(dependencies=[
            {"name": "dep", "imports": [{"module": "env", "field": "exec"}]}
        ])
    )
    assert result["ok"] is False
    assert "nested_import_denied" in codes(result)


def test_memory_max_above_host_limit_is_rejected():
    result = wia.WasmImportAllowlist(ALLOWED).validate_module(
        module(memories=[{"initial": 1, "max": 1024}])
    )
    assert result["ok"] is False
    assert "memory_limit" in codes(result)


def test_every_memory_is_limited_not_only_the_first():
    result = wia.WasmImportAllowlist(ALLOWED).validate_module(
        module(memories=[{"initial": 1, "max": 4},
                         {"initial": 1, "max": 1024}])
    )
    assert result["ok"] is False
    assert "memory_limit" in codes(result)


def test_start_function_runs_after_limits_are_installed():
    instance = wia.WasmImportAllowlist(ALLOWED).instantiate(module())
    assert instance["log"] == ["limits", "start"]


def test_start_function_cannot_exceed_memory_limit():
    policy = wia.WasmImportAllowlist(ALLOWED, max_pages=8)
    aggressive = module(memories=[{"initial": 1, "max": 8}], start_pages=8)
    assert "start_limit" in codes(policy.validate_module(aggressive))
    with pytest.raises(wia.WasmTrap):
        policy.instantiate(aggressive)


def test_grow_memory_returns_old_size_and_refuses_overshoot():
    policy = wia.WasmImportAllowlist(ALLOWED)
    instance = policy.instantiate(module(memories=[{"initial": 1, "max": 4}]))
    assert policy.grow_memory(instance, 0, 2) == 1
    assert instance["memories"][0]["pages"] == 3
    assert policy.grow_memory(instance, 0, 2) == -1
    assert instance["memories"][0]["pages"] == 3


def test_indirect_call_out_of_bounds_traps():
    policy = wia.WasmImportAllowlist(ALLOWED)
    instance = policy.instantiate(module(tables=[{"entries": ["i32->i32"]}]))
    with pytest.raises(wia.WasmTrap):
        policy.call_indirect(instance, 1, "i32->i32")


def test_indirect_call_type_mismatch_traps():
    policy = wia.WasmImportAllowlist(ALLOWED)
    instance = policy.instantiate(module(tables=[{"entries": ["i32->i32"]}]))
    with pytest.raises(wia.WasmTrap):
        policy.call_indirect(instance, 0, "i64->i64")
    assert policy.call_indirect(instance, 0, "i32->i32") == "i32->i32"


def test_preopen_resolves_paths_and_blocks_escapes():
    policy = wia.WasmImportAllowlist(ALLOWED, root="/sandbox")
    instance = policy.instantiate(module())
    with pytest.raises(wia.WasmTrap):
        policy.preopen(instance, "../etc/passwd")
    assert policy.preopen(instance, "data/../config") == "/sandbox/config"
    assert instance["preopens"] == ["/sandbox/config"]


def test_fuel_is_charged_for_every_loop_iteration():
    policy = wia.WasmImportAllowlist(ALLOWED, fuel=5)
    instance = policy.instantiate(module())
    for _ in range(5):
        policy.charge(instance, 1)
    with pytest.raises(wia.FuelExhausted):
        policy.charge(instance, 1)


def test_host_pointer_range_is_checked():
    policy = wia.WasmImportAllowlist(ALLOWED, host_pages=1)
    instance = policy.instantiate(module())
    assert policy.check_pointer(instance, 0, 65532, 4) is True
    with pytest.raises(wia.WasmTrap):
        policy.check_pointer(instance, 0, 65533, 4)
    with pytest.raises(wia.WasmTrap):
        policy.check_pointer(instance, 0, -1, 4)


def test_instance_count_is_limited():
    policy = wia.WasmImportAllowlist(ALLOWED, max_instances=2)
    policy.instantiate(module())
    policy.instantiate(module())
    with pytest.raises(wia.WasmTrap):
        policy.instantiate(module())
