import pytest

import item_binding_ledger as ibl


def make_ledger():
    ledger = ibl.ItemBindingLedger()
    ledger.add_character("hero", account_id="acct1")
    ledger.add_character("alt", account_id="acct1")
    ledger.add_character("stranger", account_id="acct2")
    return ledger


def test_equip_binding_propagates_to_socketed_gems():
    ledger = make_ledger()
    ledger.add_item("sword", owner="hero")
    ledger.add_item("gem", owner="hero")
    ledger.socket("sword", "gem")
    ledger.equip("hero", "sword")
    assert ledger.binding_of("sword") == "equip"
    assert ledger.binding_of("gem") == "equip"
    assert ledger.trade_check("gem", buyer_account="acct2")["allowed"] is False


def test_removing_gem_does_not_clear_its_binding():
    ledger = make_ledger()
    ledger.add_item("sword", owner="hero")
    ledger.add_item("gem", owner="hero")
    ledger.socket("sword", "gem")
    ledger.equip("hero", "sword")
    ledger.unsocket("sword", "gem")
    ledger.unequip("hero", "sword")
    assert ledger.binding_of("gem") == "equip"
    assert ledger.trade_check("gem")["allowed"] is False


def test_unbind_scroll_fails_on_equipped_item_without_consuming_scroll():
    ledger = make_ledger()
    ledger.add_item("sword", owner="hero")
    ledger.equip("hero", "sword")
    result = ledger.unbind("sword", operator="hero")
    assert result["unbound"] is False
    assert result["reason"] == "equipped"
    assert result["scroll_consumed"] is False
    assert ledger.binding_of("sword") == "equip"


def test_account_bound_item_cannot_be_unbound():
    ledger = make_ledger()
    ledger.add_item("relic", owner="hero", account_id="acct1", binding="account")
    result = ledger.unbind("relic", operator="hero")
    assert result["unbound"] is False
    assert result["reason"] == "account_bound"
    assert result["scroll_consumed"] is False
    assert ledger.binding_of("relic") == "account"


def test_account_bound_mail_requires_same_account_recipient():
    ledger = make_ledger()
    ledger.add_item("relic", owner="hero", account_id="acct1", binding="account")
    rejected = ledger.mail("relic", from_account="acct1", to_character="stranger")
    assert rejected["sent"] is False
    assert rejected["reason"] == "account_mismatch"
    accepted = ledger.mail("relic", from_account="acct1", to_character="alt")
    assert accepted["sent"] is True
    assert ledger.binding_of("relic") == "account"


def test_region_transfer_keeps_binding():
    ledger = make_ledger()
    ledger.add_item("sword", owner="hero")
    ledger.equip("hero", "sword")
    ledger.transfer_region("hero", "r2")
    assert ledger.character_state("hero")["region"] == "r2"
    assert ledger.binding_of("sword") == "equip"
    assert ledger.item_state("sword")["bound_to"] == "hero"


def test_binding_types_are_mutually_exclusive_and_keep_stricter_type():
    ledger = make_ledger()
    ledger.add_item("sword", owner="hero")
    assert ledger.bind("sword", "equip", source="loot", operator="hero")["applied"] is True
    downgrade = ledger.bind("sword", "trade", source="manual", operator="hero")
    assert downgrade["applied"] is False
    assert ledger.binding_of("sword") == "equip"
    upgrade = ledger.bind("sword", "account", source="manual", operator="hero")
    assert upgrade["applied"] is True
    assert ledger.binding_of("sword") == "account"
    assert ledger.trade_check("sword")["reason"] == "bound_account"


def test_propagation_never_downgrades_a_stricter_gem_binding():
    ledger = make_ledger()
    ledger.add_item("sword", owner="hero")
    ledger.add_item("gem", owner="hero", account_id="acct1", binding="account")
    ledger.socket("sword", "gem")
    ledger.equip("hero", "sword")
    assert ledger.binding_of("sword") == "equip"
    assert ledger.binding_of("gem") == "account"
    assert ledger.trade_check("gem")["reason"] == "bound_account"


def test_trade_check_returns_concrete_reason_at_listing_and_settlement():
    ledger = make_ledger()
    ledger.add_item("sword", owner="hero", binding="equip")
    listing = ledger.trade_check("sword", buyer_account="acct2", stage="list")
    settlement = ledger.trade_check("sword", buyer_account="acct2", stage="settle")
    assert listing["allowed"] is False
    assert listing["reason"] == "bound_equip"
    assert settlement["allowed"] is False
    assert settlement["reason"] == "bound_equip"


def test_item_bound_between_listing_and_settlement_is_rejected():
    ledger = make_ledger()
    ledger.add_item("sword", owner="hero")
    assert ledger.trade_check("sword", buyer_account="acct2", stage="list")["allowed"] is True
    ledger.bind("sword", "equip", source="combat", operator="hero")
    settlement = ledger.trade_check("sword", buyer_account="acct2", stage="settle")
    assert settlement["allowed"] is False
    assert settlement["reason"] == "bound_equip"


def test_enchant_binds_the_item_to_its_owner_not_the_enchanter():
    ledger = make_ledger()
    ledger.add_character("buyer", account_id="acct3")
    ledger.add_character("seller", account_id="acct4")
    ledger.add_item("buyer-sword", owner="buyer")
    ledger.add_item("seller-ring", owner="seller")
    ledger.enchant("buyer-sword", "ench-1", source_character="seller")
    assert ledger.binding_of("buyer-sword") == "equip"
    assert ledger.item_state("buyer-sword")["bound_to"] == "buyer"
    assert ledger.binding_of("seller-ring") == "none"


def test_unbind_keeps_full_event_history():
    ledger = make_ledger()
    ledger.add_item("sword", owner="hero")
    ledger.bind("sword", "equip", source="loot", operator="hero", event_id="ev-1")
    result = ledger.unbind("sword", operator="hero", event_id="ev-2")
    assert result["unbound"] is True
    assert result["scroll_consumed"] is True
    history = ledger.history("sword")
    assert [event["action"] for event in history] == ["bind", "unbind"]
    assert history[0]["source"] == "loot"
    assert history[0]["operator"] == "hero"


def test_batch_bind_updates_every_item():
    ledger = make_ledger()
    for item_id in ("a", "b", "c"):
        ledger.add_item(item_id, owner="hero")
    results = ledger.bind_batch(["a", "b", "c"], "equip", source="loot",
                                operator="hero", event_id_prefix="batch")
    assert len(results) == 3
    assert all(result["applied"] is True for result in results)
    assert [ledger.binding_of(item_id) for item_id in ("a", "b", "c")] == [
        "equip", "equip", "equip"
    ]


def test_duplicate_binding_event_is_idempotent():
    ledger = make_ledger()
    ledger.add_item("sword", owner="hero")
    first = ledger.bind("sword", "equip", source="loot", operator="hero",
                        event_id="ev-1")
    second = ledger.bind("sword", "equip", source="loot", operator="hero",
                         event_id="ev-1")
    assert first["applied"] is True
    assert second["duplicate"] is True
    assert ledger.binding_of("sword") == "equip"
    assert len(ledger.history("sword")) == 1


def test_character_deletion_clears_equip_state_and_archives_history():
    ledger = make_ledger()
    ledger.add_item("sword", owner="hero")
    ledger.equip("hero", "sword", event_id="eq-1")
    result = ledger.delete_character("hero")
    assert result["deleted"] is True
    assert ledger.binding_of("sword") == "none"
    assert ledger.item_state("sword")["equipped"] is False
    assert len(ledger.history("sword")) == 1
    assert ledger.archived_events("hero")
    ledger.add_character("hero", account_id="acct1")
    assert ledger.item_state("sword")["bound_to"] is None
    assert ledger.item_state("sword")["equipped"] is False
