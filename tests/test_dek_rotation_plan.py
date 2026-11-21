import pytest

import dek_rotation_plan as drp


def plan(**kwargs):
    options = {
        "old_version": "1",
        "new_version": "2",
        "deactivation_at": "2026-12-01T00:00:00Z",
    }
    options.update(kwargs)
    return drp.DekRotationPlan(**options)


def test_plan_records_exact_versions_and_deactivation_time():
    rotation = plan()
    rotation.add("obj-1", "tenant-a")
    status = rotation.status()
    assert status["old_version"] == "1"
    assert status["new_version"] == "2"
    assert status["deactivation_at"] == "2026-12-01T00:00:00Z"
    assert status["total"] == 1


def test_default_deactivation_time_is_recorded():
    rotation = drp.DekRotationPlan(old_version="1", new_version="2")
    rotation.add("obj-1", "tenant-a")
    assert rotation.status()["deactivation_at"] is not None


def test_unknown_dek_version_is_rejected_by_exact_match():
    rotation = plan()
    with pytest.raises(ValueError):
        rotation.add("obj-1", "tenant-a", dek_version="12")


def test_run_rewraps_objects_backups_and_indexes():
    rotation = plan()
    rotation.add("obj-1", "tenant-a", kind="object")
    rotation.add("bkp-1", "tenant-a", kind="backup")
    rotation.add("idx-1", "tenant-a", kind="index")
    status = rotation.run()
    assert status["rotated"] == 3
    assert status["complete"] is True
    assert [record["kind"] for record in rotation.audit()] == [
        "object", "backup", "index"
    ]


def test_stop_after_leaves_the_plan_incomplete():
    rotation = plan()
    for index in range(5):
        rotation.add("obj-%d" % index, "tenant-a")
    status = rotation.run(stop_after=2)
    assert status["rotated"] == 2
    assert status["pending"] == 3
    assert status["complete"] is False


def test_resume_continues_without_duplicate_audit_records():
    rotation = plan()
    for index in range(5):
        rotation.add("obj-%d" % index, "tenant-a")
    rotation.run(stop_after=2)
    status = rotation.resume()
    assert status["rotated"] == 5
    assert status["complete"] is True
    ids = [record["object_id"] for record in rotation.audit()]
    assert ids == ["obj-0", "obj-1", "obj-2", "obj-3", "obj-4"]
    assert len(ids) == len(set(ids))


def test_failed_object_stays_pending_and_blocks_disable():
    rotation = plan()
    rotation.add("obj-a", "tenant-a")
    rotation.add("obj-b", "tenant-a")
    status = rotation.run(fail_ids=("obj-b",))
    assert status["rotated"] == 1
    assert status["failed"] == 1
    assert status["complete"] is False
    with pytest.raises(RuntimeError):
        rotation.disable()


def test_disable_succeeds_after_every_object_is_rotated():
    rotation = plan()
    rotation.add("obj-a", "tenant-a")
    rotation.run()
    assert rotation.disable() is True


def test_rotation_lock_is_exclusive():
    rotation = plan()
    rotation.acquire()
    with pytest.raises(RuntimeError):
        rotation.acquire()
    rotation.release()
    rotation.acquire()
    rotation.release()


def test_idempotency_key_prevents_a_second_rewrap():
    rotation = plan()
    rotation.add("obj-a", "tenant-a")
    rotation.add("obj-b", "tenant-a")
    rotation.run(idempotency_key="rotation-1")
    rotated = rotation.status()["rotated"]
    records = len(rotation.audit())
    rotation.run(idempotency_key="rotation-1")
    assert rotation.status()["rotated"] == rotated
    assert len(rotation.audit()) == records


def test_wrapped_deks_are_isolated_per_tenant():
    rotation = plan()
    token = rotation.wrapped_dek("tenant-a", "obj-1")
    assert rotation.unwrap_dek("tenant-a", token) == "obj-1"
    with pytest.raises(PermissionError):
        rotation.unwrap_dek("tenant-b", token)


def test_audit_records_old_and_new_versions_per_object():
    rotation = plan()
    rotation.add("obj-1", "tenant-a")
    rotation.run()
    assert rotation.audit() == [
        {
            "object_id": "obj-1",
            "tenant": "tenant-a",
            "kind": "object",
            "from": "1",
            "to": "2",
        }
    ]


def test_running_a_completed_plan_is_a_no_op():
    rotation = plan()
    rotation.add("obj-1", "tenant-a")
    rotation.run()
    records = len(rotation.audit())
    status = rotation.run()
    assert status["complete"] is True
    assert len(rotation.audit()) == records


def test_status_counts_partition_the_objects():
    rotation = plan()
    rotation.add("obj-a", "tenant-a")
    rotation.add("obj-b", "tenant-a")
    rotation.add("obj-c", "tenant-a")
    status = rotation.run(fail_ids=("obj-b",))
    assert status["total"] == 3
    assert status["rotated"] == 2
    assert status["failed"] == 1
    assert status["pending"] == 0


def test_resume_retries_a_failed_object():
    rotation = plan()
    rotation.add("obj-a", "tenant-a")
    rotation.add("obj-b", "tenant-a")
    rotation.run(fail_ids=("obj-b",))
    status = rotation.resume()
    assert status["rotated"] == 2
    assert status["failed"] == 0
    assert status["complete"] is True
