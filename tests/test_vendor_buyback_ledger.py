import vendor_buyback_ledger as vbl


def item(uid="u1", price=100, **extra):
    data = {
        "uid": uid,
        "item_id": "sword",
        "name": "Sword",
        "price": price,
        "level_req": 1,
    }
    data.update(extra)
    return data


def book(**kwargs):
    return vbl.VendorBuybackLedger(**kwargs)


def test_buyback_price_is_the_sell_price_not_the_current_vendor_price():
    ledger = book()
    ledger.set_gold("hero", 1000)
    ledger.sell("v1", "hero", [item("u1", 100)], now=0.0)
    ledger.set_vendor_price("v1", "sword", 500)
    result = ledger.buyback("v1", "hero", "S1", now=1.0)
    assert result["status"] == "bought"
    assert result["charge"] == 100
    assert result["gold"] == 900
    assert result["item"]["price"] == 100


def test_buyback_consumes_the_record():
    ledger = book()
    ledger.set_gold("hero", 1000)
    ledger.sell("v1", "hero", [item()], now=0.0)
    assert ledger.buyback("v1", "hero", "S1", now=1.0)["status"] == "bought"
    second = ledger.buyback("v1", "hero", "S1", now=2.0)
    assert second["status"] == "missing"


def test_bound_items_cannot_be_sold():
    ledger = book()
    result = ledger.sell("v1", "hero", [item("u1", bound=True)], now=0.0)
    assert result["status"] == "refused"
    assert result["reason"] == "bound item"
    assert ledger.slots("v1", "hero") == []


def test_batch_sell_is_all_or_nothing():
    ledger = book()
    batch = [item("u1"), item("u2", bound=True)]
    result = ledger.sell("v1", "hero", batch, now=0.0)
    assert result["status"] == "refused"
    assert ledger.slots("v1", "hero") == []
    assert ledger.sell("v1", "hero", [item("u1")], now=1.0)["status"] == "sold"


def test_buyback_lists_are_per_vendor():
    ledger = book()
    ledger.set_gold("hero", 1000)
    ledger.sell("v1", "hero", [item()], now=0.0)
    assert ledger.buyback("v2", "hero", "S1", now=1.0)["status"] == "missing"
    assert len(ledger.slots("v1", "hero")) == 1


def test_vendor_refresh_does_not_clear_buyback_records():
    ledger = book()
    ledger.set_gold("hero", 1000)
    ledger.sell("v1", "hero", [item()], now=0.0)
    ledger.refresh_vendor("v1")
    assert len(ledger.slots("v1", "hero")) == 1
    assert ledger.buyback("v1", "hero", "S1", now=1.0)["status"] == "bought"


def test_slots_evict_the_oldest_record_first():
    ledger = book(slots=2)
    ledger.sell("v1", "hero", [item("u1")], now=0.0)
    ledger.sell("v1", "hero", [item("u2")], now=1.0)
    ledger.sell("v1", "hero", [item("u3")], now=2.0)
    records = ledger.slots("v1", "hero")
    assert [record["record_id"] for record in records] == ["S2", "S3"]
    assert ledger.buyback("v1", "hero", "S1", now=3.0)["status"] == "missing"


def test_records_survive_relogin_until_they_expire():
    ledger = book(ttl=3600.0)
    ledger.set_gold("hero", 1000)
    ledger.sell("v1", "hero", [item()], now=0.0)
    assert len(ledger.slots("v1", "hero")) == 1
    assert ledger.buyback("v1", "hero", "S1", now=50.0)["status"] == "bought"


def test_expiry_removes_the_record_and_buyback_fails():
    ledger = book(ttl=100.0)
    ledger.sell("v1", "hero", [item()], now=0.0)
    assert ledger.expire(99.0) == []
    assert ledger.expire(100.0) == ["S1"]
    assert ledger.buyback("v1", "hero", "S1", now=101.0)["status"] == "missing"
    assert ledger.slots("v1", "hero") == []


def test_bag_space_is_revalidated_before_buyback():
    ledger = book()
    ledger.set_gold("hero", 1000)
    ledger.sell("v1", "hero", [item()], now=0.0)
    result = ledger.buyback("v1", "hero", "S1", now=1.0, bag_free=0)
    assert result["status"] == "no_space"
    assert len(ledger.slots("v1", "hero")) == 1
    assert ledger.buyback("v1", "hero", "S1", now=2.0, bag_free=1)["status"] == "bought"


def test_level_requirement_is_revalidated_before_buyback():
    ledger = book()
    ledger.set_gold("hero", 1000)
    ledger.sell("v1", "hero", [item("u1", level_req=10)], now=0.0)
    result = ledger.buyback("v1", "hero", "S1", now=1.0, level=5)
    assert result["status"] == "level_too_low"
    assert result["required_level"] == 10
    assert ledger.buyback("v1", "hero", "S1", now=2.0, level=10)["status"] == "bought"


def test_insufficient_gold_leaves_the_record_and_balance_untouched():
    ledger = book()
    ledger.set_gold("hero", 50)
    ledger.sell("v1", "hero", [item()], now=0.0)
    result = ledger.buyback("v1", "hero", "S1", now=1.0)
    assert result["status"] == "insufficient_funds"
    assert ledger.gold_of("hero") == 50
    assert len(ledger.slots("v1", "hero")) == 1
    ledger.set_gold("hero", 100)
    bought = ledger.buyback("v1", "hero", "S1", now=2.0)
    assert bought["status"] == "bought"
    assert bought["gold"] == 0


def test_item_snapshot_keeps_upgrade_and_enchant():
    ledger = book()
    ledger.set_gold("hero", 1000)
    sold = item("u1", price=100, upgrade=3, enchant="fire")
    ledger.sell("v1", "hero", [sold], now=0.0)
    result = ledger.buyback("v1", "hero", "S1", now=1.0)
    assert result["item"]["upgrade"] == 3
    assert result["item"]["enchant"] == "fire"
    assert result["item"]["name"] == "Sword"


def test_one_item_instance_cannot_be_sold_twice_while_active():
    ledger = book()
    first = ledger.sell("v1", "hero", [item("u1")], now=0.0)
    second = ledger.sell("v1", "hero", [item("u1")], now=1.0)
    assert first["status"] == "sold"
    assert second["status"] == "refused"
    assert second["reason"] == "duplicate item"
    assert len(ledger.slots("v1", "hero")) == 1


def test_record_ids_are_shared_and_never_reused():
    ledger = book()
    ledger.set_gold("hero", 500)
    first = ledger.sell("v1", "hero", [item("u1", 100)], now=0.0)
    assert first["records"][0]["record_id"] == "S1"
    bought = ledger.buyback("v1", "hero", "S1", now=1.0)
    assert bought["gold"] == 400
    second = ledger.sell("v1", "hero", [item("u2", 100)], now=2.0)
    assert second["records"][0]["record_id"] == "S2"
    assert [record["record_id"] for record in ledger.history_of("hero")] == [
        "S1",
        "S2",
    ]
