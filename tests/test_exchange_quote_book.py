import exchange_quote_book as eqb


def book(**kwargs):
    kwargs.setdefault("fee_bps", 200)
    kwargs.setdefault("cache_seconds", 10.0)
    kwargs.setdefault("daily_limit", 100000)
    engine = eqb.ExchangeQuoteBook(**kwargs)
    engine.set_rate(("gold", "diamond"), 2, 1)
    engine.set_rate(("diamond", "gold"), 1, 1)
    engine.set_balance("acct", "gold", 10000)
    engine.set_balance("acct", "diamond", 0)
    return engine


def test_expired_quote_cannot_execute():
    engine = book()
    quote = engine.quote("acct", "gold", "diamond", 100, now=0.0)
    result = engine.execute(quote["quote_id"], now=10.0)
    assert result["status"] == "expired"
    assert engine.balances("acct")["gold"] == 10000


def test_execute_uses_the_version_locked_rate():
    engine = book()
    quote = engine.quote("acct", "gold", "diamond", 100, now=0.0)
    engine.set_rate(("gold", "diamond"), 3, 2)
    result = engine.execute(quote["quote_id"], now=1.0)
    assert result["status"] == "executed"
    assert result["quote_version"] == 1
    assert result["fee_breakdown"]["gross"] == 200
    assert engine.balances("acct")["diamond"] == 196


def test_fee_is_computed_once_on_the_total_with_a_visible_remainder():
    engine = book()
    quote = engine.quote("acct", "gold", "diamond", 333, now=0.0)
    result = engine.execute(quote["quote_id"], now=1.0)
    breakdown = result["fee_breakdown"]
    assert breakdown["gross"] == 666
    assert breakdown["fee"] == 13
    assert breakdown["fee_remainder"] == 3200
    assert breakdown["net"] == 653


def test_duplicate_execute_does_not_debit_twice():
    engine = book()
    quote = engine.quote("acct", "gold", "diamond", 100, now=0.0)
    first = engine.execute(quote["quote_id"], now=1.0)
    second = engine.execute(quote["quote_id"], now=2.0)
    assert first["status"] == "executed"
    assert second["status"] == "already_executed"
    assert engine.balances("acct")["gold"] == 9900


def test_insufficient_balance_rolls_back_both_sides():
    engine = book()
    engine.set_balance("acct", "gold", 50)
    quote = engine.quote("acct", "gold", "diamond", 100, now=0.0)
    result = engine.execute(quote["quote_id"], now=1.0)
    assert result["status"] == "insufficient_funds"
    assert engine.balances("acct") == {"gold": 50, "diamond": 0}


def test_credit_failure_rolls_back_the_debit():
    engine = book()
    quote = engine.quote("acct", "gold", "diamond", 500, now=0.0)
    result = engine.execute(quote["quote_id"], now=1.0, fail_credit=True)
    assert result["status"] == "failed"
    assert result["reason"] == "credit_failed"
    assert engine.balances("acct") == {"gold": 10000, "diamond": 0}
    assert engine.execute(quote["quote_id"], now=2.0)["status"] == "executed"


def test_daily_limit_counts_amount_not_request_count():
    engine = book(fee_bps=0, daily_limit=1000)
    engine.set_rate(("gold", "diamond"), 1, 1)
    engine.set_balance("acct", "gold", 2000)
    first = engine.quote("acct", "gold", "diamond", 400, now=0.0)
    second = engine.quote("acct", "gold", "diamond", 350, now=0.1)
    third = engine.quote("acct", "gold", "diamond", 300, now=0.2)
    engine.execute(first["quote_id"], now=1.0)
    engine.execute(second["quote_id"], now=2.0)
    result = engine.execute(third["quote_id"], now=3.0)
    assert result["status"] == "limit_exceeded"
    assert engine.balances("acct")["gold"] == 1250
    assert engine.limits("acct")["gold"] == {"used": 750, "remaining": 250, "limit": 1000}


def test_idempotency_key_is_globally_unique():
    engine = book()
    first = engine.quote("acct", "gold", "diamond", 100, now=0.0, idempotency_key="k-1")
    second = engine.quote(
        "other", "gold", "diamond", 999, now=1.0, idempotency_key="k-1"
    )
    assert second["quote_id"] == first["quote_id"]
    assert second["amount"] == 100
    assert second["account"] == "acct"


def test_maintenance_rejects_quote_and_execution():
    engine = book()
    quote = engine.quote("acct", "gold", "diamond", 100, now=0.0)
    engine.set_maintenance(True)
    assert engine.quote("acct", "gold", "diamond", 100, now=1.0)["status"] == "maintenance"
    assert engine.execute(quote["quote_id"], now=1.0)["status"] == "maintenance"
    engine.set_maintenance(False)
    assert engine.execute(quote["quote_id"], now=2.0)["status"] == "executed"


def test_roundtrip_never_creates_money_after_fees():
    engine = book()
    engine.set_rate(("gold", "diamond"), 1, 1)
    engine.set_rate(("diamond", "gold"), 1, 1)
    engine.set_balance("acct", "gold", 1000)
    first = engine.quote("acct", "gold", "diamond", 1000, now=0.0)
    engine.execute(first["quote_id"], now=1.0)
    second = engine.quote("acct", "diamond", "gold", 980, now=2.0)
    engine.execute(second["quote_id"], now=3.0)
    balances = engine.balances("acct")
    assert balances["gold"] == 961
    assert balances["gold"] < 1000
    assert balances["diamond"] == 0


def test_balances_are_copies_and_hide_intermediate_states():
    engine = book()
    quote = engine.quote("acct", "gold", "diamond", 500, now=0.0)
    engine.execute(quote["quote_id"], now=1.0, fail_credit=True)
    view = engine.balances("acct")
    view["gold"] = -1
    assert engine.balances("acct")["gold"] == 10000
    assert engine.balances("acct")["diamond"] == 0


def test_cached_quote_returns_the_same_result_within_its_window():
    engine = book()
    first = engine.quote("acct", "gold", "diamond", 100, now=0.0)
    engine.set_rate(("gold", "diamond"), 3, 2)
    cached = engine.quote("acct", "gold", "diamond", 100, now=2.0)
    assert cached["quote_id"] == first["quote_id"]
    assert cached["rate"] == 2
    fresh = engine.quote("acct", "gold", "diamond", 100, now=11.0)
    assert fresh["quote_id"] != first["quote_id"]
    assert fresh["rate"] == 3
    assert fresh["version"] == 2


def test_cancelled_quote_cannot_execute():
    engine = book()
    quote = engine.quote("acct", "gold", "diamond", 100, now=0.0)
    assert engine.cancel(quote["quote_id"])["status"] == "cancelled"
    result = engine.execute(quote["quote_id"], now=1.0)
    assert result["status"] == "cancelled"
    assert engine.balances("acct")["gold"] == 10000


def test_ledger_records_version_price_and_fee_breakdown():
    engine = book()
    quote = engine.quote("acct", "gold", "diamond", 250, now=0.0)
    engine.execute(quote["quote_id"], now=1.0)
    entries = engine.ledger("acct")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["quote_version"] == 1
    assert entry["rate"] == 2
    assert entry["gross"] == 500
    assert entry["fee"] == 10
    assert entry["net"] == 490
    assert entry["fee_remainder"] == 0


def test_batch_execution_is_atomic_and_uses_the_same_limit_rules():
    engine = book(fee_bps=0, daily_limit=1000)
    engine.set_rate(("gold", "diamond"), 1, 1)
    engine.set_balance("acct", "gold", 500)
    first = engine.quote("acct", "gold", "diamond", 300, now=0.0)
    second = engine.quote("acct", "gold", "diamond", 250, now=0.1)
    result = engine.execute_batch([first["quote_id"], second["quote_id"]], now=1.0)
    assert result["status"] == "failed"
    assert result["reason"] == "insufficient_funds"
    assert engine.balances("acct")["gold"] == 500
    assert engine.ledger("acct") == []
    assert engine.execute(first["quote_id"], now=2.0)["status"] == "executed"
    assert engine.balances("acct")["gold"] == 200
