"""Currency exchange quote book with locked quotes and atomic settlement."""

import copy
import threading


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
        self.fee_bps = fee_bps
        self.cache_seconds = cache_seconds
        self.daily_limit = daily_limit
        self._maintenance = bool(maintenance)
        self._rates = {}
        self._balances = {}
        self._quotes = {}
        self._quote_cache = {}
        self._idempotency = {}
        self._used = {}
        self._ledger = []
        self._next_quote_id = 1
        self._lock = threading.RLock()

        if rates:
            for pair, value in rates.items():
                if isinstance(value, dict):
                    self.set_rate(pair, value["rate"], value.get("version", 1))
                elif isinstance(value, (tuple, list)):
                    version = value[1] if len(value) > 1 else 1
                    self.set_rate(pair, value[0], version)
                else:
                    self.set_rate(pair, value, 1)

    @staticmethod
    def _pair_key(pair):
        if isinstance(pair, (tuple, list)) and len(pair) == 2:
            return (pair[0], pair[1])
        if isinstance(pair, str):
            for separator in ("->", "/", ":", "_"):
                if separator in pair:
                    left, right = pair.split(separator, 1)
                    return (left, right)
        raise ValueError("pair must contain exactly two currencies")

    @staticmethod
    def _fee_breakdown(amount, rate, fee_bps):
        gross = amount * rate
        scaled_fee = gross * fee_bps
        fee = scaled_fee // 10000
        fee_remainder = scaled_fee % 10000
        return {
            "gross": gross,
            "fee": fee,
            "fee_remainder": fee_remainder,
            "net": gross - fee,
        }

    def _quote_view(self, quote, now=None):
        view = copy.deepcopy(quote)
        if view["status"] == "active" and now is not None and now >= view["expires_at"]:
            view["status"] = "expired"
        return view

    def _account_balances(self, account, balances=None):
        source = self._balances if balances is None else balances
        return source.setdefault(account, {})

    def _limit_info(self, account, currency, used=None):
        used_by_currency = self._used if used is None else used
        current = used_by_currency.get((account, currency), 0)
        remaining = None if self.daily_limit is None else max(0, self.daily_limit - current)
        return {"used": current, "remaining": remaining, "limit": self.daily_limit}

    def _limit_exceeded(self, account, currency, amount, used=None):
        if self.daily_limit is None:
            return False
        used_by_currency = self._used if used is None else used
        current = used_by_currency.get((account, currency), 0)
        return current + amount > self.daily_limit

    def _ledger_entry(self, quote, breakdown, now):
        return {
            "account": quote["account"],
            "to_account": quote["account"],
            "quote_id": quote["quote_id"],
            "idempotency_key": quote.get("idempotency_key"),
            "quote_version": quote["version"],
            "from_ccy": quote["from_ccy"],
            "to_ccy": quote["to_ccy"],
            "amount": quote["amount"],
            "rate": quote["rate"],
            "gross": breakdown["gross"],
            "fee": breakdown["fee"],
            "fee_remainder": breakdown["fee_remainder"],
            "net": breakdown["net"],
            "fee_breakdown": copy.deepcopy(breakdown),
            "created_at": quote["created_at"],
            "executed_at": now,
        }

    def set_balance(self, account, currency, amount):
        with self._lock:
            self._account_balances(account)[currency] = amount

    def balances(self, account):
        with self._lock:
            return copy.deepcopy(self._balances.get(account, {}))

    def set_rate(self, pair, rate, version):
        with self._lock:
            self._rates[self._pair_key(pair)] = {"rate": rate, "version": version}

    def set_maintenance(self, on):
        with self._lock:
            self._maintenance = bool(on)

    def quote(self, account, from_ccy, to_ccy, amount, now=0.0, idempotency_key=None):
        with self._lock:
            if self._maintenance:
                return {
                    "status": "maintenance",
                    "account": account,
                    "from_ccy": from_ccy,
                    "to_ccy": to_ccy,
                    "amount": amount,
                }

            if idempotency_key is not None and idempotency_key in self._idempotency:
                quote = self._quotes[self._idempotency[idempotency_key]]
                return self._quote_view(quote, now)

            if amount <= 0:
                return {
                    "status": "failed",
                    "reason": "invalid_amount",
                    "account": account,
                    "from_ccy": from_ccy,
                    "to_ccy": to_ccy,
                    "amount": amount,
                }

            rate_info = self._rates.get((from_ccy, to_ccy))
            if rate_info is None:
                return {
                    "status": "failed",
                    "reason": "rate_not_found",
                    "account": account,
                    "from_ccy": from_ccy,
                    "to_ccy": to_ccy,
                    "amount": amount,
                }

            cache_key = (account, from_ccy, to_ccy, amount)
            cached_id = self._quote_cache.get(cache_key)
            if cached_id is not None:
                cached = self._quotes[cached_id]
                if cached["status"] == "active" and now < cached["expires_at"]:
                    if idempotency_key is not None:
                        self._idempotency[idempotency_key] = cached_id
                    return self._quote_view(cached, now)

            quote_id = "q-{}".format(self._next_quote_id)
            self._next_quote_id += 1
            quote = {
                "quote_id": quote_id,
                "account": account,
                "from_ccy": from_ccy,
                "to_ccy": to_ccy,
                "amount": amount,
                "rate": rate_info["rate"],
                "version": rate_info["version"],
                "fee_bps": self.fee_bps,
                "created_at": now,
                "expires_at": now + self.cache_seconds,
                "status": "active",
                "idempotency_key": idempotency_key,
            }
            self._quotes[quote_id] = quote
            self._quote_cache[cache_key] = quote_id
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = quote_id
            return self._quote_view(quote)

    def cancel(self, quote_id, now=0.0):
        with self._lock:
            if self._maintenance:
                return {"status": "maintenance", "quote_id": quote_id}

            quote = self._quotes.get(quote_id)
            if quote is None:
                return {"status": "failed", "reason": "quote_not_found", "quote_id": quote_id}
            if quote["status"] == "executed":
                return {"status": "already_executed", "quote_id": quote_id}
            if quote["status"] == "cancelled":
                return {"status": "cancelled", "quote_id": quote_id}
            if now >= quote["expires_at"]:
                return {"status": "expired", "quote_id": quote_id}

            quote["status"] = "cancelled"
            quote["cancelled_at"] = now
            return {"status": "cancelled", "quote_id": quote_id}

    def execute(self, quote_id, now=0.0, fail_credit=False):
        with self._lock:
            if self._maintenance:
                return {"status": "maintenance", "quote_id": quote_id}

            quote = self._quotes.get(quote_id)
            if quote is None:
                return {"status": "failed", "reason": "quote_not_found", "quote_id": quote_id}

            if quote["status"] == "executed":
                return {
                    "status": "already_executed",
                    "quote_id": quote_id,
                    "quote_version": quote["version"],
                    "fee_breakdown": copy.deepcopy(quote["fee_breakdown"]),
                    "balances": self.balances(quote["account"]),
                }
            if quote["status"] == "cancelled":
                return {"status": "cancelled", "quote_id": quote_id}
            if now >= quote["expires_at"]:
                return {"status": "expired", "quote_id": quote_id}

            account = quote["account"]
            from_ccy = quote["from_ccy"]
            to_ccy = quote["to_ccy"]
            amount = quote["amount"]
            account_balances = self._balances.get(account, {})
            source_balance = account_balances.get(from_ccy, 0)

            if source_balance < amount:
                return {
                    "status": "insufficient_funds",
                    "quote_id": quote_id,
                    "balances": self.balances(account),
                }
            if self._limit_exceeded(account, from_ccy, amount):
                return {
                    "status": "limit_exceeded",
                    "quote_id": quote_id,
                    "currency": from_ccy,
                    "limits": self._limit_info(account, from_ccy),
                    "balances": self.balances(account),
                }
            if fail_credit:
                return {
                    "status": "failed",
                    "reason": "credit_failed",
                    "quote_id": quote_id,
                    "balances": self.balances(account),
                }

            breakdown = self._fee_breakdown(amount, quote["rate"], quote["fee_bps"])
            destination_balance = account_balances.get(to_ccy, 0)
            self._account_balances(account)[from_ccy] = source_balance - amount
            self._account_balances(account)[to_ccy] = destination_balance + breakdown["net"]
            self._used[(account, from_ccy)] = self._used.get((account, from_ccy), 0) + amount

            quote["status"] = "executed"
            quote["executed_at"] = now
            quote["fee_breakdown"] = copy.deepcopy(breakdown)
            self._ledger.append(self._ledger_entry(quote, breakdown, now))

            return {
                "status": "executed",
                "quote_id": quote_id,
                "quote_version": quote["version"],
                "rate": quote["rate"],
                "fee_breakdown": copy.deepcopy(breakdown),
                "balances": self.balances(account),
            }

    def execute_batch(self, quote_ids, now=0.0):
        with self._lock:
            if self._maintenance:
                return {"status": "maintenance"}

            unique_ids = []
            seen = set()
            for quote_id in quote_ids:
                if quote_id not in seen:
                    seen.add(quote_id)
                    unique_ids.append(quote_id)

            local_balances = copy.deepcopy(self._balances)
            local_used = dict(self._used)
            pending_quotes = []
            breakdowns = []

            def fail(reason, quote_id):
                return {
                    "status": "failed",
                    "reason": reason,
                    "failed_quote": quote_id,
                    "balances": copy.deepcopy(self._balances),
                }

            for quote_id in unique_ids:
                quote = self._quotes.get(quote_id)
                if quote is None:
                    return fail("quote_not_found", quote_id)
                if quote["status"] == "executed":
                    return fail("already_executed", quote_id)
                if quote["status"] == "cancelled":
                    return fail("cancelled", quote_id)
                if now >= quote["expires_at"]:
                    return fail("expired", quote_id)

                account = quote["account"]
                from_ccy = quote["from_ccy"]
                to_ccy = quote["to_ccy"]
                amount = quote["amount"]
                account_balances = self._account_balances(account, local_balances)
                source_balance = account_balances.get(from_ccy, 0)

                if source_balance < amount:
                    return fail("insufficient_funds", quote_id)
                if self._limit_exceeded(account, from_ccy, amount, local_used):
                    return fail("limit_exceeded", quote_id)

                breakdown = self._fee_breakdown(amount, quote["rate"], quote["fee_bps"])
                account_balances[from_ccy] = source_balance - amount
                account_balances[to_ccy] = account_balances.get(to_ccy, 0) + breakdown["net"]
                local_used[(account, from_ccy)] = local_used.get((account, from_ccy), 0) + amount
                pending_quotes.append(quote)
                breakdowns.append(breakdown)

            self._balances = local_balances
            self._used = local_used

            entries = []
            results = []
            for quote, breakdown in zip(pending_quotes, breakdowns):
                quote["status"] = "executed"
                quote["executed_at"] = now
                quote["fee_breakdown"] = copy.deepcopy(breakdown)
                entry = self._ledger_entry(quote, breakdown, now)
                entries.append(entry)
                results.append(
                    {
                        "quote_id": quote["quote_id"],
                        "quote_version": quote["version"],
                        "rate": quote["rate"],
                        "fee_breakdown": copy.deepcopy(breakdown),
                    }
                )
            self._ledger.extend(entries)

            total_breakdown = {
                "gross": sum(item["gross"] for item in breakdowns),
                "fee": sum(item["fee"] for item in breakdowns),
                "fee_remainder": sum(item["fee_remainder"] for item in breakdowns),
                "net": sum(item["net"] for item in breakdowns),
            }
            return {
                "status": "executed",
                "results": results,
                "fee_breakdown": total_breakdown,
                "balances": copy.deepcopy(self._balances.get(pending_quotes[0]["account"], {}))
                if pending_quotes
                else {},
            }

    def limits(self, account, currency=None):
        with self._lock:
            if currency is not None:
                return self._limit_info(account, currency)

            currencies = {
                ccy for (acct, ccy), _amount in self._used.items()
                if acct == account
            }
            return {
                ccy: self._limit_info(account, ccy)
                for ccy in sorted(currencies)
            }

    def ledger(self, account):
        with self._lock:
            return [
                copy.deepcopy(entry)
                for entry in self._ledger
                if entry["account"] == account or entry["to_account"] == account
            ]
