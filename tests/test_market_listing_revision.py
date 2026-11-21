import market_listing_revision as mlr


def book(**kwargs):
    return mlr.MarketListingRevision(**kwargs)


def item():
    return {"item_id": "sword", "name": "Runed Sword"}


def test_buy_with_a_stale_version_is_rejected():
    market = book()
    market.list_item("L1", "seller", item(), 100, 5, now=0.0)
    result = market.buy("L1", "buyer", 1, seen_version=2, now=1.0)
    assert result["status"] == "stale_version"
    assert result["listing_version"] == 1
    assert market.settle("L1")["sales"] == []


def test_buy_charges_the_price_of_the_seen_version():
    market = book()
    market.list_item("L1", "seller", item(), 100, 5, now=0.0)
    assert market.revise("L1", 120, expected_version=1, now=0.0)["revised"] is True
    result = market.buy("L1", "buyer", 5, seen_version=2, now=1.0)
    assert result["status"] == "sold"
    sale = market.settle("L1")["sales"][0]
    assert sale["version"] == 2
    assert sale["price"] == 120
    assert sale["total"] == 600


def test_cancel_and_buy_are_mutually_exclusive():
    market = book()
    market.list_item("L1", "seller", item(), 100, 5, now=0.0)
    assert market.cancel("L1", now=1.0)["status"] == "cancelled"
    result = market.buy("L1", "buyer", 1, seen_version=1, now=2.0)
    assert result["status"] == "unavailable"
    assert market.settle("L1")["sales"] == []


def test_buy_then_cancel_does_not_return_the_item_twice():
    market = book()
    market.list_item("L1", "seller", item(), 100, 1, now=0.0)
    assert market.buy("L1", "buyer", 1, seen_version=1, now=1.0)["status"] == "sold"
    result = market.cancel("L1", now=2.0)
    assert result["status"] == "unavailable"
    assert market.mails("L1") == []


def test_a_sold_listing_cannot_be_bought_twice():
    market = book()
    market.list_item("L1", "seller", item(), 100, 1, now=0.0)
    market.buy("L1", "buyer", 1, seen_version=1, now=1.0)
    second = market.buy("L1", "other", 1, seen_version=1, now=2.0)
    assert second["status"] == "unavailable"
    assert len(market.settle("L1")["sales"]) == 1


def test_partial_fill_creates_a_remaining_listing_on_the_version_chain():
    market = book(fee_bps=0)
    market.list_item("L1", "seller", item(), 100, 5, now=0.0)
    result = market.buy("L1", "buyer", 2, seen_version=1, now=1.0)
    assert result["status"] == "partial"
    remaining_id = result["remaining_listing_id"]
    remaining = market.get_listing(remaining_id)
    assert remaining["version"] == 2
    assert remaining["price"] == 100
    assert remaining["quantity"] == 3
    sold = market.buy(remaining_id, "buyer2", 3, seen_version=2, now=2.0)
    assert sold["status"] == "sold"
    assert market.settle("L1")["sales"][0]["total"] == 200
    assert market.settle(remaining_id)["sales"][0]["total"] == 300


def test_tax_is_computed_once_on_the_sale_total():
    market = book(fee_bps=500)
    market.list_item("L1", "seller", item(), 1000, 2, now=0.0)
    result = market.buy("L1", "buyer", 2, seen_version=1, now=1.0)
    assert result["total"] == 2000
    assert result["fee"] == 100
    assert result["seller_net"] == 1900


def test_revision_without_a_version_is_rejected():
    market = book()
    market.list_item("L1", "seller", item(), 100, 5, now=0.0)
    result = market.revise("L1", 130, expected_version=None, now=1.0)
    assert result["revised"] is False
    assert result["reason"] == "missing version"
    assert market.get_listing("L1")["price"] == 100


def test_a_late_revision_cannot_overwrite_a_newer_price():
    market = book()
    market.list_item("L1", "seller", item(), 100, 5, now=0.0)
    market.revise("L1", 120, expected_version=1, now=0.0)
    late = market.revise("L1", 130, expected_version=1, now=1.0)
    assert late["revised"] is False
    assert late["reason"] == "stale version"
    listing = market.get_listing("L1")
    assert listing["version"] == 2
    assert listing["price"] == 120


def test_revision_cooldown_is_enforced():
    market = book(revise_cooldown=10.0)
    market.list_item("L1", "seller", item(), 100, 5, now=0.0)
    assert market.revise("L1", 110, 1, now=0.0)["revised"] is True
    blocked = market.revise("L1", 120, 2, now=5.0)
    assert blocked["revised"] is False
    assert blocked["reason"] == "cooldown"
    assert blocked["retry_at"] == 10.0
    assert market.revise("L1", 120, 2, now=10.0)["revised"] is True


def test_revision_count_limit_is_enforced():
    market = book(max_revisions=2)
    market.list_item("L1", "seller", item(), 100, 5, now=0.0)
    assert market.revise("L1", 110, 1, now=0.0)["version"] == 2
    assert market.revise("L1", 120, 2, now=1.0)["version"] == 3
    blocked = market.revise("L1", 130, 3, now=2.0)
    assert blocked["revised"] is False
    assert blocked["reason"] == "revision limit"
    assert market.get_listing("L1")["version"] == 3


def test_expiry_and_manual_cancel_send_only_one_return_mail():
    market = book()
    market.list_item("L1", "seller", item(), 100, 5, now=0.0, ttl=100.0)
    assert market.expire(99.0) == []
    assert market.expire(100.0) == ["L1"]
    assert len(market.mails("L1")) == 1
    result = market.cancel("L1", now=101.0)
    assert result["status"] == "expired"
    assert len(market.mails("L1")) == 1


def test_offline_seller_cancel_still_returns_the_item_once():
    market = book()
    market.list_item("L1", "seller", item(), 100, 5, now=0.0)
    result = market.cancel("L1", now=1.0, seller_offline=True)
    assert result["status"] == "cancelled"
    assert len(market.mails("L1")) == 1
    assert market.buy("L1", "buyer", 1, 1, now=2.0)["status"] == "unavailable"


def test_version_chain_replays_every_revision():
    market = book(fee_bps=0)
    market.list_item("L1", "seller", item(), 100, 5, now=0.0)
    market.revise("L1", 120, 1, now=0.0)
    result = market.buy("L1", "buyer", 2, 2, now=1.0)
    remaining_id = result["remaining_listing_id"]
    assert [row["version"] for row in market.history("L1")] == [1, 2]
    assert [row["version"] for row in market.history(remaining_id)] == [1, 2, 3]
    assert market.get_listing(remaining_id)["price"] == 120


def test_listing_view_and_settlement_carry_the_current_version():
    market = book(fee_bps=0)
    market.list_item("L1", "seller", item(), 100, 2, now=0.0)
    market.revise("L1", 120, 1, now=0.0)
    view = market.get_listing("L1")
    assert view["version"] == 2
    assert view["price"] == 120
    view["price"] = 1
    assert market.get_listing("L1")["price"] == 120
    market.buy("L1", "buyer", 2, 2, now=1.0)
    sale = market.settle("L1")["sales"][0]
    assert sale["listing_id"] == "L1"
    assert sale["version"] == 2
