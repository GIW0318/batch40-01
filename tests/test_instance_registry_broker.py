import instance_registry_broker as irb


def broker(**kwargs):
    kwargs.setdefault("capacity", 2)
    kwargs.setdefault("heartbeat_timeout", 30.0)
    kwargs.setdefault("grace", 10.0)
    kwargs.setdefault("id_space", 4)
    return irb.InstanceRegistryBroker(**kwargs)


def test_two_parties_racing_for_the_last_slot_do_not_share_an_instance():
    book = broker(capacity=1)
    first = book.acquire("alpha", ["a1", "a2"], now=0.0)
    second = book.acquire("beta", ["b1", "b2"], now=0.0)
    assert first["status"] == "allocated"
    assert first["members"] == ["a1", "a2"]
    assert second["status"] == "queued"
    assert second["position"] == 1
    assert "beta" not in book.parties


def test_party_can_only_hold_one_active_instance():
    book = broker()
    first = book.acquire("alpha", ["a1", "a2"], now=0.0)
    again = book.acquire("alpha", ["a1", "a2"], now=1.0)
    assert again["status"] == "existing"
    assert again["instance_id"] == first["instance_id"]
    assert book.capacity()["assigned"] == 1


def test_release_is_refused_while_a_member_is_still_online():
    book = broker()
    instance = book.acquire("alpha", ["a1", "a2"], now=0.0)
    first = book.release(instance["instance_id"], "alpha", member_id="a1", now=1.0)
    assert first["released"] is False
    assert first["online"] == ["a2"]
    second = book.release(instance["instance_id"], "alpha", member_id="a2", now=2.0)
    assert second["released"] is True
    assert second["status"] == "grace"


def test_recycle_waits_for_the_grace_period():
    book = broker(capacity=1)
    instance = book.acquire("alpha", ["a1"], now=0.0)
    book.release(instance["instance_id"], "alpha", now=0.0)
    assert book.reap(5.0)["closed"] == []
    assert book.capacity()["assigned"] == 1
    closed = book.reap(10.0)["closed"]
    assert closed == [instance["instance_id"]]
    assert book.capacity()["available"] == 1


def test_reconnect_within_grace_restores_the_same_instance_and_progress():
    book = broker()
    instance = book.acquire("alpha", ["a1", "a2"], now=0.0)
    book.record_progress(instance["instance_id"], "boss-1")
    book.release(instance["instance_id"], "alpha", now=0.0)
    restored = book.reconnect(
        "alpha", instance["instance_id"], ["a1", "a2"], now=5.0
    )
    assert restored["status"] == "reconnected"
    assert restored["instance_id"] == instance["instance_id"]
    assert restored["progress"] == ["boss-1"]


def test_reconnect_after_grace_is_gone():
    book = broker()
    instance = book.acquire("alpha", ["a1"], now=0.0)
    book.release(instance["instance_id"], "alpha", now=0.0)
    result = book.reconnect("alpha", instance["instance_id"], ["a1"], now=10.0)
    assert result["status"] == "gone"
    assert book.capacity()["available"] == 2


def test_roster_change_requires_the_original_leader_to_hand_over():
    book = broker()
    instance = book.acquire("alpha", ["a1", "a2", "a3"], now=0.0)
    mismatch = book.reconnect(
        "alpha", instance["instance_id"], ["a1", "a2"], now=1.0
    )
    assert mismatch["status"] == "roster_mismatch"
    handover = book.transfer_leader(
        instance["instance_id"], "a2", new_members=["a2", "a3"]
    )
    assert handover["status"] == "transferred"
    accepted = book.reconnect(
        "alpha", instance["instance_id"], ["a2", "a3"], now=2.0
    )
    assert accepted["status"] == "reconnected"


def test_reused_instance_id_does_not_expose_another_partys_progress():
    book = broker(capacity=2, id_space=2)
    first = book.acquire("alpha", ["a1"], now=0.0)
    book.acquire("beta", ["b1"], now=0.0)
    book.release(first["instance_id"], "alpha", now=0.0)
    book.reap(10.0)
    reused = book.acquire("gamma", ["g1"], now=11.0)
    assert reused["instance_id"] == first["instance_id"]
    book.record_progress(reused["instance_id"], "boss-2")
    stale = book.reconnect("alpha", first["instance_id"], ["a1"], now=12.0)
    assert stale["status"] == "party_mismatch"
    assert "progress" not in stale


def test_capacity_reports_assigned_and_queued_separately():
    book = broker(capacity=2)
    book.acquire("alpha", ["a1"], now=0.0)
    book.acquire("beta", ["b1"], now=0.0)
    assert book.capacity() == {
        "assigned": 2,
        "queued": 0,
        "available": 0,
        "capacity": 2,
    }
    book.acquire("gamma", ["g1"], now=1.0)
    assert book.capacity()["queued"] == 1
    book.release(book.parties["alpha"], "alpha", now=2.0)
    assert book.capacity()["assigned"] == 2
    assert book.capacity()["available"] == 0


def test_close_broadcasts_to_members_before_recycling():
    book = broker()
    instance = book.acquire("alpha", ["a1", "a2"], now=0.0)
    result = book.close(instance["instance_id"], now=1.0)
    assert result["status"] == "closed"
    assert sorted(result["broadcasts"]) == ["a1", "a2"]
    assert book.capacity()["available"] == 2
    assert book.reconnect("alpha", instance["instance_id"], ["a1"], now=2.0)["status"] == "gone"


def test_difficulty_change_does_not_reuse_the_old_instance():
    book = broker()
    instance = book.acquire("alpha", ["a1"], difficulty="normal", now=0.0)
    result = book.acquire("alpha", ["a1"], difficulty="heroic", now=1.0)
    assert result["status"] == "difficulty_mismatch"
    assert result["instance_id"] == instance["instance_id"]
    assert result["active_difficulty"] == "normal"
    assert book.capacity()["assigned"] == 1


def test_heartbeat_timeout_recycles_the_instance():
    book = broker(capacity=1)
    instance = book.acquire("alpha", ["a1"], now=0.0)
    assert book.reap(31.0)["closed"] == [instance["instance_id"]]
    assert book.capacity()["available"] == 1


def test_pending_reclaim_mark_resumes_after_restart():
    book = broker()
    instance = book.acquire("alpha", ["a1"], now=0.0)
    book.release(instance["instance_id"], "alpha", now=0.0)
    state = book.export_state()

    restarted = irb.InstanceRegistryBroker()
    restarted.load_state(state)
    result = restarted.reap(10.0)
    assert result["closed"] == [instance["instance_id"]]
    assert restarted.capacity()["available"] == 2


def test_queued_party_is_promoted_when_a_slot_frees():
    book = broker(capacity=1)
    first = book.acquire("alpha", ["a1"], now=0.0)
    book.acquire("beta", ["b1"], now=0.0)
    book.release(first["instance_id"], "alpha", now=0.0)
    result = book.reap(10.0)
    assert result["promoted"] == ["beta"]
    instance_id = book.parties["beta"]
    record = book.instances[instance_id]
    assert record["status"] == "active"
    assert record["members"] == ["b1"]
    assert book.capacity()["queued"] == 0


def test_heartbeat_cancels_a_pending_reclaim():
    book = broker()
    instance = book.acquire("alpha", ["a1", "a2"], now=0.0)
    book.release(instance["instance_id"], "alpha", now=0.0)
    revived = book.heartbeat(
        instance["instance_id"], "alpha", ["a1", "a2"], now=5.0
    )
    assert revived["status"] == "active"
    assert book.reap(20.0)["closed"] == []
    restored = book.reconnect(
        "alpha", instance["instance_id"], ["a1", "a2"], now=21.0
    )
    assert restored["status"] == "reconnected"
