import pytest

import guild_bank_audit as gba


def make_bank():
    bank = gba.GuildBankAudit(max_limit=1000, timezone="+08:00")
    bank.add_member("master", role="master", daily_limit=1000)
    bank.add_member("officer", role="officer", daily_limit=10)
    bank.add_member("member", role="member", can_withdraw=False, daily_limit=10)
    bank.add_member("owner", role="member", daily_limit=10)
    bank.add_member("thief", role="member", daily_limit=10)
    return bank


def test_stale_slot_version_blocks_second_withdrawal():
    bank = make_bank()
    bank.deposit("master", "materials", 1, 5, "ore")
    version = bank.slot_state("materials", 1)["version"]
    first = bank.withdraw("officer", "materials", 1, 3, expected_version=version)
    assert first["quantity"] == 2
    with pytest.raises(ValueError):
        bank.withdraw("officer", "materials", 1, 1, expected_version=version)
    assert bank.slot_state("materials", 1)["quantity"] == 2


def test_daily_limit_counts_item_quantity_not_operations():
    bank = make_bank()
    bank.deposit("master", "materials", 1, 20, "ore")
    bank.withdraw("officer", "materials", 1, 8)
    bank.withdraw("officer", "materials", 1, 2)
    with pytest.raises(ValueError):
        bank.withdraw("officer", "materials", 1, 1)
    assert bank.slot_state("materials", 1)["quantity"] == 10


def test_revoked_permission_invalidates_already_open_panel():
    bank = make_bank()
    bank.deposit("master", "materials", 1, 5, "ore")
    panel = bank.open_panel("officer")
    assert panel["can_withdraw"] is True
    bank.set_permission("officer", can_withdraw=False)
    with pytest.raises(ValueError):
        bank.withdraw("officer", "materials", 1, 1)
    assert bank.slot_state("materials", 1)["quantity"] == 5


def test_bound_item_can_only_be_withdrawn_by_its_owner():
    bank = make_bank()
    bank.deposit("owner", "soulbound", 1, 1, "heirloom", bound_to="owner")
    with pytest.raises(ValueError):
        bank.withdraw("thief", "soulbound", 1, 1)
    result = bank.withdraw("owner", "soulbound", 1, 1)
    assert result["quantity"] == 0


def test_dissolve_mails_remaining_items_to_leader_and_snapshots_assets():
    bank = make_bank()
    bank.deposit("master", "materials", 1, 5, "ore")
    bank.deposit("master", "consumables", 2, 3, "potion")
    result = bank.dissolve("master")
    assert result["dissolved"] is True
    assert result["mailed"] == 2
    queued = bank.mail_queue()
    assert len(queued) == 2
    assert {entry["to"] for entry in queued} == {"master"}
    assert sorted(entry["item_id"] for entry in queued) == ["ore", "potion"]
    assert bank.slot_state("materials", 1)["empty"] is True
    assert bank.slot_state("consumables", 2)["empty"] is True


def test_dissolve_is_idempotent_and_does_not_duplicate_mail():
    bank = make_bank()
    bank.deposit("master", "materials", 1, 5, "ore")
    first = bank.dissolve("master")
    second = bank.dissolve("master")
    assert second["snapshot_id"] == first["snapshot_id"]
    assert len(bank.mail_queue()) == 1
    assert len(bank.asset_snapshots_list()) == 1


def test_audit_log_records_actor_tab_and_quantities():
    bank = make_bank()
    bank.deposit("officer", "materials", 1, 5, "ore", now=100.0)
    bank.withdraw("officer", "materials", 1, 2, now=200.0)
    entries = bank.log(tab="materials", page=1)
    deposit = [entry for entry in entries if entry["action"] == "deposit"][0]
    withdraw = [entry for entry in entries if entry["action"] == "withdraw"][0]
    assert deposit["actor"] == "officer"
    assert deposit["before"] == 0
    assert deposit["after"] == 5
    assert withdraw["before"] == 5
    assert withdraw["after"] == 3


def test_repair_charge_is_idempotent_by_request_id():
    bank = make_bank()
    bank.add_funds(1000)
    first = bank.charge_repair("master", 100, request_id="repair-1")
    second = bank.charge_repair("master", 100, request_id="repair-1")
    assert first["charged"] == 100
    assert second["duplicate"] is True
    assert bank.guild_funds() == 900


def test_cross_page_move_is_logged_with_both_locations():
    bank = make_bank()
    bank.deposit("master", "materials", 1, 4, "ore")
    bank.move("officer", "materials", 1, "materials", 2, 3)
    assert bank.slot_state("materials", 1)["quantity"] == 1
    assert bank.slot_state("materials", 2)["quantity"] == 3
    entry = bank.log()[-1]
    assert entry["action"] == "move"
    assert entry["from_page"] == 1
    assert entry["to_page"] == 2
    assert entry["before"] == 4
    assert entry["after"] == 1


def test_master_cannot_raise_own_limit_above_maximum():
    bank = make_bank()
    with pytest.raises(ValueError):
        bank.set_limit("master", "master", 5000)
    bank.set_limit("master", "officer", 5)
    bank.deposit("master", "materials", 1, 10, "ore")
    with pytest.raises(ValueError):
        bank.withdraw("officer", "materials", 1, 6)


def test_expansion_keeps_old_log_page_references():
    bank = make_bank()
    bank.deposit("master", "materials", 1, 5, "ore", now=10.0)
    before = bank.log(page=1)
    assert len(before) == 1
    bank.expand("master", 3)
    assert bank.page_count() == 3
    after = bank.log(page=1)
    assert len(after) == 1
    assert after[0]["page"] == 1
    assert after[0]["seq"] == before[0]["seq"]
    assert bank.log(page=2) == []


def test_duplicate_deposit_request_is_idempotent():
    bank = make_bank()
    first = bank.deposit("officer", "materials", 1, 5, "ore", request_id="dep-1")
    second = bank.deposit("officer", "materials", 1, 5, "ore", request_id="dep-1")
    assert first["quantity"] == 5
    assert second["duplicate"] is True
    assert bank.slot_state("materials", 1)["quantity"] == 5


def test_withdrawing_more_than_available_changes_nothing():
    bank = make_bank()
    bank.deposit("master", "materials", 1, 2, "ore")
    version = bank.slot_state("materials", 1)["version"]
    with pytest.raises(ValueError):
        bank.withdraw("officer", "materials", 1, 3)
    assert bank.slot_state("materials", 1)["quantity"] == 2
    assert bank.slot_state("materials", 1)["version"] == version


def test_role_without_withdraw_permission_is_denied():
    bank = make_bank()
    bank.deposit("master", "materials", 1, 5, "ore")
    with pytest.raises(ValueError):
        bank.withdraw("member", "materials", 1, 1)
    assert bank.slot_state("materials", 1)["quantity"] == 5


def test_log_uses_server_time_and_timezone():
    bank = make_bank()
    bank.deposit("master", "materials", 1, 1, "ore", now=1234.5)
    entry = bank.log()[-1]
    assert entry["at"] == 1234.5
    assert entry["timezone"] == "+08:00"
