import vault_compaction_job as vcj


def job(stack_limit=20):
    return vcj.VaultCompactionJob(stack_limit=stack_limit)


def slot_counts(snapshot, item_id):
    counts = []
    for page in snapshot["pages"]:
        for slot in page:
            if slot is not None and slot["item_id"] == item_id:
                counts.append(slot["count"])
    return counts


def test_compaction_merges_stacks_up_to_the_limit_without_losing_items():
    engine = job(stack_limit=20)
    engine.create_vault("v", pages=1, page_size=4)
    engine.place("v", {"item_id": "ore", "count": 15})
    engine.place("v", {"item_id": "ore", "count": 15})
    engine.compact("v", pages=[0])
    snapshot = engine.snapshot("v")
    assert slot_counts(snapshot, "ore") == [20, 10]
    assert sum(slot_counts(snapshot, "ore")) == 30


def test_readonly_page_is_never_touched_by_compaction():
    engine = job()
    engine.create_vault("v", pages=2, page_size=4)
    engine.place("v", {"item_id": "ore", "count": 5}, page=0)
    engine.place("v", {"item_id": "ore", "count": 5}, page=1)
    engine.set_page_readonly("v", 0)
    engine.compact("v", pages=[0, 1])
    snapshot = engine.snapshot("v")
    assert snapshot["pages"][0][0] == {"item_id": "ore", "count": 5}
    assert snapshot["pages"][1][0] == {"item_id": "ore", "count": 5}
    assert sum(slot_counts(snapshot, "ore")) == 10


def test_locked_items_are_skipped_by_compaction():
    engine = job()
    engine.create_vault("v", pages=1, page_size=4)
    engine.place("v", {"item_id": "ore", "count": 5})
    engine.place("v", {"item_id": "ore", "count": 5})
    engine.set_locked("v", "ore")
    engine.compact("v", pages=[0])
    snapshot = engine.snapshot("v")
    assert snapshot["pages"][0][0] == {"item_id": "ore", "count": 5}
    assert snapshot["pages"][0][1] == {"item_id": "ore", "count": 5}


def test_place_during_compaction_goes_to_pending_and_is_not_lost():
    engine = job()
    engine.create_vault("v", pages=1, page_size=4)
    engine.place("v", {"item_id": "ore", "count": 20})
    task = engine.start_compact("v", pages=[0])
    queued = engine.place("v", {"item_id": "iron", "count": 3})
    assert queued["status"] == "pending"
    assert queued["task_id"] == task["task_id"]
    result = engine.finish_compact(task["task_id"])
    snapshot = engine.snapshot("v")
    assert slot_counts(snapshot, "iron") == [3]
    assert result["pending_placed"] == 1


def test_failure_restores_the_pre_compaction_snapshot():
    engine = job()
    engine.create_vault("v", pages=1, page_size=4)
    engine.place("v", {"item_id": "ore", "count": 15})
    engine.place("v", {"item_id": "ore", "count": 15})
    before = engine.snapshot("v")
    result = engine.compact("v", pages=[0], fail=True)
    assert result["status"] == "failed"
    assert engine.snapshot("v") == before


def test_retrying_the_same_task_is_idempotent():
    engine = job()
    engine.create_vault("v", pages=1, page_size=4)
    engine.place("v", {"item_id": "ore", "count": 15})
    engine.place("v", {"item_id": "ore", "count": 15})
    task = engine.start_compact("v", pages=[0])
    first = engine.finish_compact(task["task_id"])
    second = engine.finish_compact(task["task_id"])
    snapshot = engine.snapshot("v")
    assert second["status"] == "already_finished"
    assert second["version_after"] == first["version_after"]
    assert sum(slot_counts(snapshot, "ore")) == 30
    assert len(slot_counts(snapshot, "ore")) == 2


def test_a_second_tab_cannot_compact_while_the_first_is_running():
    engine = job()
    engine.create_vault("v", pages=2, page_size=2)
    task = engine.start_compact("v", pages=[0])
    blocked = engine.start_compact("v", pages=[1])
    assert blocked["status"] == "busy"
    assert blocked["task_id"] == task["task_id"]
    assert engine.finish_compact(task["task_id"])["status"] == "finished"


def test_same_account_cannot_compact_two_realms_at_once():
    engine = job()
    engine.create_vault("a", pages=1, page_size=2, account_id="acc", realm="r1")
    engine.create_vault("b", pages=1, page_size=2, account_id="acc", realm="r2")
    first = engine.start_compact("a", pages=[0])
    blocked = engine.start_compact("b", pages=[0])
    assert blocked["status"] == "account_busy"
    engine.finish_compact(first["task_id"])
    assert engine.start_compact("b", pages=[0])["status"] == "started"


def test_expand_preserves_existing_page_indices():
    engine = job()
    engine.create_vault("v", pages=1, page_size=2)
    engine.place("v", {"item_id": "sword", "count": 1})
    engine.expand("v", 1)
    snapshot = engine.snapshot("v")
    assert len(snapshot["pages"]) == 2
    assert snapshot["pages"][0][0] == {"item_id": "sword", "count": 1}
    assert snapshot["pages"][1] == [None, None]


def test_snapshot_during_compaction_reports_the_old_version():
    engine = job()
    engine.create_vault("v", pages=1, page_size=4)
    engine.place("v", {"item_id": "ore", "count": 20})
    task = engine.start_compact("v", pages=[0])
    snapshot = engine.snapshot("v")
    assert snapshot["compacting"] is True
    assert snapshot["version"] == task["version_before"]
    assert snapshot["pages"][0][0] == {"item_id": "ore", "count": 20}
    engine.finish_compact(task["task_id"])


def test_snapshot_after_compaction_reports_the_new_version():
    engine = job()
    engine.create_vault("v", pages=1, page_size=4)
    task = engine.start_compact("v", pages=[0])
    result = engine.finish_compact(task["task_id"])
    snapshot = engine.snapshot("v")
    assert snapshot["compacting"] is False
    assert snapshot["version"] == result["version_after"]
    assert result["version_after"] == result["version_before"] + 1


def test_capacity_recounts_real_occupancy_and_writes_the_index():
    engine = job()
    engine.create_vault("v", pages=1, page_size=4)
    engine.place("v", {"item_id": "ore", "count": 15})
    engine.place("v", {"item_id": "ore", "count": 15})
    engine.compact("v", pages=[0])
    capacity = engine.capacity("v")
    snapshot = engine.snapshot("v")
    assert capacity["used"] == 2
    assert capacity["capacity"] == 4
    assert capacity["free"] == 2
    assert snapshot["index"]["used"] == capacity["used"]


def test_operation_list_can_replay_the_compacted_layout():
    engine = job()
    engine.create_vault("v", pages=1, page_size=4)
    engine.place("v", {"item_id": "ore", "count": 15})
    engine.place("v", {"item_id": "ore", "count": 15})
    task = engine.start_compact("v", pages=[0])
    engine.finish_compact(task["task_id"])
    operations = engine.operations(task["task_id"])
    assert operations
    for operation in operations:
        assert "from" in operation
        assert "to" in operation
        assert operation["item_id"] == "ore"
    replay = engine.replay(task["task_id"])
    assert replay["pages"] == engine.snapshot("v")["pages"]


def test_empty_pages_are_removed_after_compaction():
    engine = job()
    engine.create_vault("v", pages=2, page_size=2)
    engine.place("v", {"item_id": "ore", "count": 5}, page=0)
    engine.compact("v", pages=[0, 1])
    snapshot = engine.snapshot("v")
    assert len(snapshot["pages"]) == 1
    assert snapshot["pages"][0][0] == {"item_id": "ore", "count": 5}


def test_place_overflow_is_returned_instead_of_being_swallowed():
    engine = job(stack_limit=20)
    engine.create_vault("v", pages=1, page_size=1)
    first = engine.place("v", {"item_id": "ore", "count": 25})
    second = engine.place("v", {"item_id": "ore", "count": 5})
    assert first["placed"] == 20
    assert first["overflow"] == 5
    assert second["placed"] == 0
    assert second["overflow"] == 5
    snapshot = engine.snapshot("v")
    assert snapshot["pages"][0][0]["count"] == 20
