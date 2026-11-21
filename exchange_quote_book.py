"""exchange_quote_book (initial skeleton)."""


class ExchangeQuoteBook:
    """Currency exchange quote book.

    Rates are set per ordered pair with a version.  quote returns quote_id,
    account, from_ccy, to_ccy, amount, rate, version, fee_bps, created_at,
    expires_at and status.  A cached quote for the same account, pair and amount is
    returned unchanged while now < expires_at, and a quote is expired once
    now >= expires_at; an idempotency key is globally
    unique.  execute settles at the quote's locked rate.  gross = amount * rate,
    fee = gross * fee_bps // 10000, fee_remainder = gross * fee_bps % 10000 and
    net = gross - fee.  Statuses include executed, already_executed, cancelled,
    expired, insufficient_funds, limit_exceeded, maintenance and failed.  Debit
    and credit are atomic; fail_credit must roll back.  Daily limits accumulate by
    currency amount, not by request count.  execute_batch is all-or-nothing and
    uses the same fee and limit rules.  limits returns used, remaining and limit
    per currency.  Ledger entries carry quote_id, quote_version, rate, gross, fee,
    fee_remainder and net.  A successful execute returns status, quote_version,
    fee_breakdown and balances.  A failed execute with fail_credit reports status
    failed and reason credit_failed; a failed batch reports failed_quote and
    reason.
    """

    def __init__(self, rates=None, fee_bps=200, cache_seconds=10.0, daily_limit=100000, maintenance=False):
        raise NotImplementedError()

    def set_balance(self, account, currency, amount):
        raise NotImplementedError()

    def balances(self, account):
        raise NotImplementedError()

    def set_rate(self, pair, rate, version):
        raise NotImplementedError()

    def set_maintenance(self, on):
        raise NotImplementedError()

    def quote(self, account, from_ccy, to_ccy, amount, now=0.0, idempotency_key=None):
        raise NotImplementedError()

    def cancel(self, quote_id, now=0.0):
        raise NotImplementedError()

    def execute(self, quote_id, now=0.0, fail_credit=False):
        raise NotImplementedError()

    def execute_batch(self, quote_ids, now=0.0):
        raise NotImplementedError()

    def limits(self, account, currency=None):
        raise NotImplementedError()

    def ledger(self, account):
        raise NotImplementedError()
