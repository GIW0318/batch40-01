import hashlib
import json

import pytest

import cloudsave_conflict_merge as ccm


def checksum(data, deleted=False, schema_version=2):
    payload = {
        "data": data,
        "deleted": deleted,
        "schema_version": schema_version,
    }
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def service():
    return ccm.CloudsaveConflictMerge(schema_version=2)


def test_parallel_version_vectors_create_conflict():
    svc = service()
    svc.upload("acct", "c1", {"level": 1}, {"pc": 2, "phone": 1},
               updated_at=10)
    result = svc.upload("acct", "c1", {"level": 2}, {"pc": 1, "phone": 2},
                        updated_at=20)
    assert result["status"] == "conflict"
    assert "conflict_id" in result
    assert svc.download("acct", "c1")["data"] == {"level": 1}


def test_dominating_upload_is_stored():
    svc = service()
    svc.upload("acct", "c1", {"level": 1}, {"pc": 1})
    result = svc.upload("acct", "c1", {"level": 2}, {"pc": 2})
    assert result["status"] == "stored"
    saved = svc.download("acct", "c1")
    assert saved["data"] == {"level": 2}
    assert saved["schema_version"] == 2
    assert saved["character_id"] == "c1"


def test_stale_upload_is_rejected():
    svc = service()
    svc.upload("acct", "c1", {"level": 2}, {"pc": 2})
    result = svc.upload("acct", "c1", {"level": 1}, {"pc": 1})
    assert result["status"] == "stale"
    assert svc.download("acct", "c1")["data"] == {"level": 2}


def test_equal_vectors_use_server_time_fallback():
    svc = service()
    svc.upload("acct", "c1", {"level": 1}, {"pc": 1}, updated_at=100)
    result = svc.upload("acct", "c1", {"level": 2}, {"pc": 1},
                        updated_at=200)
    assert result["status"] == "stored"
    assert svc.download("acct", "c1")["data"] == {"level": 2}


def test_equal_vectors_with_older_time_are_stale():
    svc = service()
    svc.upload("acct", "c1", {"level": 2}, {"pc": 1}, updated_at=200)
    result = svc.upload("acct", "c1", {"level": 1}, {"pc": 1},
                        updated_at=100)
    assert result["status"] == "stale"
    assert svc.download("acct", "c1")["data"] == {"level": 2}


def test_incomplete_upload_stays_pending():
    svc = service()
    svc.upload("acct", "c1", {"level": 1}, {"pc": 1})
    result = svc.upload("acct", "c1", {"level": 2}, {"pc": 2},
                        complete=False)
    assert result["status"] == "pending"
    assert svc.download("acct", "c1")["data"] == {"level": 1}


def test_checksum_mismatch_is_corrupt():
    svc = service()
    svc.upload("acct", "c1", {"level": 1}, {"pc": 1})
    result = svc.upload(
        "acct",
        "c1",
        {"level": 2},
        {"pc": 2},
        checksum="cafe",
        expected_checksum=checksum({"level": 2}),
    )
    assert result["status"] == "corrupt"
    assert svc.download("acct", "c1")["data"] == {"level": 1}


def test_merge_unions_completed_tasks():
    svc = service()
    merged = svc.merge(
        {},
        {"level": 3, "completed_tasks": ["a"]},
        {"level": 5, "completed_tasks": ["b", "a"]},
    )
    assert merged["completed_tasks"] == ["a", "b"]
    assert merged["level"] == 5


def test_merge_keeps_max_numeric_and_equipment():
    svc = service()
    merged = svc.merge(
        {},
        {"equipment": {"sword": 3, "shield": 1}},
        {"equipment": {"sword": 5, "boots": 2}},
    )
    assert merged["equipment"]["sword"] == 5
    assert merged["equipment"]["shield"] == 1
    assert merged["equipment"]["boots"] == 2


def test_merge_preserves_new_schema_fields():
    svc = service()
    merged = svc.merge({"old": 1}, {"old": 2}, {"new_field": 3})
    assert merged["old"] == 2
    assert merged["new_field"] == 3


def test_delete_tombstone_blocks_old_upload():
    svc = service()
    svc.upload("acct", "c1", {"level": 1}, {"pc": 1})
    result = svc.upload("acct", "c1", None, {"pc": 3}, deleted=True)
    assert result["status"] == "deleted"
    assert svc.download("acct", "c1")["deleted"] is True
    assert svc.download("acct", "c1")["data"] is None
    stale = svc.upload("acct", "c1", {"level": 1}, {"pc": 2})
    assert stale["status"] == "stale"
    assert svc.download("acct", "c1")["deleted"] is True


def test_characters_are_isolated():
    svc = service()
    svc.upload("acct", "warrior", {"bag": ["sword"]}, {"pc": 1})
    svc.upload("acct", "mage", {"bag": ["staff"]}, {"pc": 1})
    assert svc.download("acct", "warrior")["data"] == {"bag": ["sword"]}
    assert svc.download("acct", "mage")["data"] == {"bag": ["staff"]}


def test_readonly_session_creates_draft():
    svc = service()
    assert svc.begin_session("acct", "c1", "s1")["role"] == "writer"
    svc.upload("acct", "c1", {"level": 1}, {"pc": 1}, session_id="s1")
    assert svc.begin_session("acct", "c1", "s2")["role"] == "readonly"
    result = svc.upload("acct", "c1", {"level": 9}, {"pc": 9},
                        session_id="s2")
    assert result["status"] == "draft"
    assert "conflict_id" in result
    assert svc.download("acct", "c1")["data"] == {"level": 1}


def test_end_session_allows_new_writer():
    svc = service()
    assert svc.begin_session("acct", "c1", "s1")["role"] == "writer"
    svc.end_session("acct", "c1")
    assert svc.begin_session("acct", "c1", "s2")["role"] == "writer"
    result = svc.upload("acct", "c1", {"level": 2}, {"pc": 1},
                        session_id="s2")
    assert result["status"] == "stored"


def test_resolve_merges_conflict_draft():
    svc = service()
    svc.upload("acct", "c1", {"level": 5, "completed_tasks": ["a"]},
               {"pc": 2}, updated_at=10)
    conflict = svc.upload(
        "acct",
        "c1",
        {"level": 8, "completed_tasks": ["b"]},
        {"phone": 2},
        updated_at=20,
    )
    resolved = svc.resolve("acct", "c1", "default",
                           conflict["conflict_id"])
    assert resolved["status"] == "resolved"
    assert resolved["data"]["level"] == 8
    assert resolved["data"]["completed_tasks"] == ["a", "b"]
    assert svc.download("acct", "c1")["data"]["completed_tasks"] == ["a", "b"]
