import raid_lockout_ledger as rll


def ledger():
    return rll.RaidLockoutLedger(period_seconds=100.0)


def test_same_group_reentering_gets_the_same_lockout_id():
    book = ledger()
    first = book.enter("tower", "normal", "g1", "chr-a", 250.0)
    second = book.enter("tower", "normal", "g1", "chr-b", 260.0)
    assert first["lockout_id"] == second["lockout_id"]
    assert second["period"] == 2
    assert second["expires_at"] == 300.0
    assert second["bosses"] == []


def test_different_groups_do_not_share_boss_progress():
    book = ledger()
    book.enter("tower", "normal", "g1", "chr-a", 250.0)
    book.record_kill("tower", "normal", "g1", "boss-1", True, 251.0)
    other = book.enter("tower", "normal", "g2", "chr-b", 252.0)
    assert other["lockout_id"] != "tower:normal:2:g1"
    assert other["bosses"] == []
    assert len(book.query("tower", "normal", "g1", now=252.0)["bosses"]) == 1


def test_normal_and_heroic_lockouts_are_separate():
    book = ledger()
    normal = book.enter("tower", "normal", "g1", "chr-a", 250.0)
    heroic = book.enter("tower", "heroic", "g1", "chr-a", 250.0)
    book.record_kill("tower", "normal", "g1", "boss-1", True, 251.0)
    assert normal["lockout_id"] != heroic["lockout_id"]
    assert book.query("tower", "heroic", "g1", now=251.0)["bosses"] == []
    assert len(book.query("tower", "normal", "g1", now=251.0)["bosses"]) == 1


def test_extend_moves_expiry_to_the_end_of_the_next_period():
    book = ledger()
    book.enter("tower", "normal", "g1", "chr-a", 250.0)
    result = book.extend("tower", "normal", "g1", 260.0)
    assert result["extended"] is True
    assert result["expires_at"] == 400.0
    assert book.query("tower", "normal", "g1", now=260.0)["expires_at"] == 400.0


def test_extend_is_refused_after_the_lockout_expired():
    book = ledger()
    book.enter("tower", "normal", "g1", "chr-a", 250.0)
    result = book.extend("tower", "normal", "g1", 310.0)
    assert result["extended"] is False
    assert result["reason"] == "expired"
    assert book.query("tower", "normal", "g1", now=250.0)["expires_at"] == 300.0


def test_wipe_is_not_recorded_as_a_boss_kill():
    book = ledger()
    book.enter("tower", "normal", "g1", "chr-a", 250.0)
    result = book.record_kill("tower", "normal", "g1", "boss-1", False, 251.0)
    assert result["recorded"] is False
    assert result["reason"] == "not confirmed"
    assert book.query("tower", "normal", "g1", now=251.0)["bosses"] == []
    assert book.can_loot("tower", "normal", "g1", "boss-1", now=251.0) is True


def test_duplicate_boss_report_is_ignored():
    book = ledger()
    book.enter("tower", "normal", "g1", "chr-a", 250.0)
    first = book.record_kill("tower", "normal", "g1", "boss-1", True, 251.0)
    second = book.record_kill("tower", "normal", "g1", "boss-1", True, 252.0)
    assert first["recorded"] is True
    assert second["recorded"] is False
    assert second["reason"] == "duplicate"
    assert len(book.query("tower", "normal", "g1", now=252.0)["bosses"]) == 1


def test_reset_uses_the_server_period_boundary():
    book = ledger()
    lockout = book.enter("tower", "normal", "g1", "chr-a", 250.0)
    assert book.reset(299.0) == []
    assert book.query("tower", "normal", "g1", now=299.0) is not None
    assert book.reset(300.0) == [lockout["lockout_id"]]
    assert book.query("tower", "normal", "g1", now=300.0) is None


def test_locked_boss_cannot_be_looted_again():
    book = ledger()
    book.enter("tower", "normal", "g1", "chr-a", 250.0)
    assert book.can_loot("tower", "normal", "g1", "boss-1", now=250.0) is True
    book.record_kill("tower", "normal", "g1", "boss-1", True, 251.0)
    assert book.can_loot("tower", "normal", "g1", "boss-1", now=251.0) is False
    assert book.can_loot("tower", "normal", "g1", "boss-2", now=251.0) is True


def test_query_returns_an_independent_copy():
    book = ledger()
    book.enter("tower", "normal", "g1", "chr-a", 250.0)
    book.record_kill("tower", "normal", "g1", "boss-1", True, 251.0)
    snapshot = book.query("tower", "normal", "g1", now=251.0)
    snapshot["bosses"][0]["boss_id"] = "hacked"
    snapshot["members"].append("intruder")
    again = book.query("tower", "normal", "g1", now=251.0)
    assert again["bosses"][0]["boss_id"] == "boss-1"
    assert "intruder" not in again["members"]


def test_boss_detail_carries_kill_time_and_source_group():
    book = ledger()
    book.enter("tower", "normal", "g1", "chr-a", 250.0)
    book.record_kill(
        "tower", "normal", "g1", "boss-1", True, 260.0, source_group="g1-alpha"
    )
    boss = book.query("tower", "normal", "g1", now=260.0)["bosses"][0]
    assert boss["killed_at"] == 260.0
    assert boss["source_group"] == "g1-alpha"


def test_same_account_characters_in_different_groups_stay_independent():
    book = ledger()
    book.enter("tower", "normal", "g1", "alt-main", 250.0)
    book.record_kill("tower", "normal", "g1", "boss-1", True, 251.0)
    second = book.enter("tower", "normal", "g2", "alt-alt", 252.0)
    assert second["bosses"] == []
    assert len(book.query("tower", "normal", "g1", now=252.0)["bosses"]) == 1


def test_transfer_does_not_carry_the_old_groups_progress():
    book = ledger()
    book.enter("tower", "normal", "g1", "chr-a", 250.0)
    book.record_kill("tower", "normal", "g1", "boss-1", True, 251.0)
    result = book.transfer("chr-a", "g1", "g2", "tower", "normal", 260.0)
    assert result["transferred"] is True
    target = book.query("tower", "normal", "g2", now=260.0)
    assert target["bosses"] == []
    assert "chr-a" in target["members"]
    old = book.query("tower", "normal", "g1", now=260.0)
    assert len(old["bosses"]) == 1
    assert "chr-a" not in old["members"]


def test_failed_transfer_leaves_no_partial_progress():
    book = ledger()
    book.enter("tower", "normal", "g1", "chr-a", 250.0)
    before = book.export_state()
    result = book.transfer(
        "chr-a", "g1", "g2", "tower", "normal", 260.0, fail=True
    )
    assert result["transferred"] is False
    assert book.export_state() == before


def test_extend_writes_an_audit_entry_and_state_survives_restart():
    book = ledger()
    book.enter("tower", "normal", "g1", "chr-a", 250.0)
    book.record_kill("tower", "normal", "g1", "boss-1", True, 251.0)
    book.extend("tower", "normal", "g1", 260.0)
    audit = book.audit()
    assert len(audit) == 1
    assert audit[0]["action"] == "extend"
    assert audit[0]["new_expires_at"] == 400.0

    restored = rll.RaidLockoutLedger()
    restored.load_state(book.export_state())
    record = restored.query("tower", "normal", "g1", now=260.0)
    assert len(record["bosses"]) == 1
    assert record["expires_at"] == 400.0
    assert restored.audit() == audit
