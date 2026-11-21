import pytest

import cgroup_limit_guard as clg


def guard():
    return clg.CgroupLimitGuard()


def test_memory_limit_below_current_usage_is_rejected():
    g = guard()
    g.create_group("/team")
    g.set_usage("/team", memory=100)
    with pytest.raises(ValueError):
        g.set_limits("/team", {"memory.max": 50})
    assert g.get("/team")["memory.max"] is None


def test_memory_limit_at_or_above_usage_is_accepted():
    g = guard()
    g.create_group("/team")
    g.set_usage("/team", memory=100)
    assert g.set_limits("/team", {"memory.max": 100})["memory.max"] == 100
    assert g.set_limits("/team", {"memory.max": 150})["memory.max"] == 150


def test_high_watermark_above_hard_limit_is_rejected():
    g = guard()
    g.create_group("/team", limits={"memory.max": 100})
    with pytest.raises(ValueError):
        g.set_limits("/team", {"memory.high": 200})
    assert g.get("/team")["memory.high"] is None
    assert g.set_limits("/team", {"memory.high": 80})["memory.high"] == 80


def test_swap_zero_means_disabled_not_unlimited():
    g = guard()
    g.create_group("/team")
    updated = g.set_limits("/team", {"memory.swap.max": "0"})
    assert updated["memory.swap.max"] == 0
    assert updated["memory.max"] is None
    assert g.set_limits("/team", {"memory.swap.max": "max"})["memory.swap.max"] is None


def test_pids_max_is_distinguished_from_zero():
    g = guard()
    g.create_group("/team")
    g.set_usage("/team", pids=5)
    assert g.parse_limit("pids.max", "max") is None
    assert g.set_limits("/team", {"pids.max": "max"})["pids.max"] is None
    assert g.can_fork("/team", count=3) is True
    assert g.parse_limit("pids.max", "0") == 0
    assert g.set_limits("/team", {"pids.max": "0"})["pids.max"] == 0
    assert g.can_fork("/team", count=1) is False


def test_child_memory_limit_cannot_exceed_parent():
    g = guard()
    g.create_group("/team", limits={"memory.max": 100})
    g.create_group("/team/a")
    with pytest.raises(ValueError):
        g.set_limits("/team/a", {"memory.max": 200})
    assert g.get("/team/a")["memory.max"] is None
    assert g.set_limits("/team/a", {"memory.max": 100})["memory.max"] == 100


def test_cpu_max_parses_quota_and_period_positionally():
    g = guard()
    assert g.parse_limit("cpu.max", "50000 100000") == (50000, 100000)
    assert g.parse_limit("cpu.max", "max 100000") == (None, 100000)
    with pytest.raises(ValueError):
        g.parse_limit("cpu.max", "100000 50000")
    with pytest.raises(ValueError):
        g.parse_limit("cpu.max", "50000 500")
    with pytest.raises(ValueError):
        g.parse_limit("cpu.max", "500 100000")
    g.create_group("/team")
    assert g.set_limits("/team", {"cpu.max": "50000 100000"})["cpu.max"] == (50000, 100000)


def test_delegation_requires_controller_availability():
    g = guard()
    g.create_group("/team", controllers=["cpu"])
    with pytest.raises(ValueError):
        g.delegate("/team", "1000:1000", ["memory"])
    node = g.get("/team")
    assert node["owner"] is None
    assert node["subtree_control"] == []


def test_delegation_sets_owner_and_enables_controllers():
    g = guard()
    g.create_group("/team", controllers=["cpu", "memory"])
    result = g.delegate("/team", "1000:1000", ["cpu", "memory"])
    assert result["delegated"] is True
    node = g.get("/team")
    assert node["owner"] == "1000:1000"
    assert node["subtree_control"] == ["cpu", "memory"]


def test_stats_aggregate_direct_children_without_double_counting():
    g = guard()
    g.create_group("/a")
    g.create_group("/a/b")
    g.set_usage("/", memory=1, pids=1)
    g.set_usage("/a", memory=2, pids=2)
    g.set_usage("/a/b", memory=3, pids=3)
    assert g.stats("/") == {"memory": 6, "pids": 6}
    assert g.stats("/a") == {"memory": 5, "pids": 5}
    assert g.stats("/a/b") == {"memory": 3, "pids": 3}


def test_unsafe_group_names_are_rejected():
    g = guard()
    g.create_group("/team")
    with pytest.raises(ValueError):
        g.create_group("/team/bad name")
    with pytest.raises(ValueError):
        g.create_group("/team/escaped/child")
    with pytest.raises(KeyError):
        g.get("/team/escaped/child")


def test_path_traversal_is_rejected():
    g = guard()
    g.create_group("/team")
    with pytest.raises(ValueError):
        g.create_group("/team/../etc")
    with pytest.raises(ValueError):
        g.create_group("/team/./sub")


def test_batch_changes_roll_back_on_failure():
    g = guard()
    g.create_group("/a", limits={"memory.max": 500})
    g.create_group("/b")
    g.set_usage("/b", memory=50)
    with pytest.raises(ValueError):
        g.set_many([
            {"path": "/a", "limits": {"memory.max": 200}},
            {"path": "/b", "limits": {"memory.max": 10}},
        ])
    assert g.get("/a")["memory.max"] == 500
    assert g.get("/b")["memory.max"] is None


def test_unknown_limit_key_is_rejected_without_partial_apply():
    g = guard()
    g.create_group("/a", limits={"memory.max": 500})
    with pytest.raises(ValueError):
        g.set_limits("/a", {"memory.max": 300, "memory.min": 10})
    assert g.get("/a")["memory.max"] == 500


def test_single_set_limits_is_all_or_nothing():
    g = guard()
    g.create_group("/a", limits={"memory.max": 500})
    with pytest.raises(ValueError):
        g.set_limits("/a", {"memory.max": 300, "memory.high": 400})
    node = g.get("/a")
    assert node["memory.max"] == 500
    assert node["memory.high"] is None
    updated = g.set_limits("/a", {"memory.max": 300, "memory.high": 200})
    assert updated["memory.max"] == 300
    assert updated["memory.high"] == 200
