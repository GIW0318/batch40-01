"""Vendor buyback ledger.

Each (vendor_id, character_id) keeps an ordered list of at most ``slots``
records; the oldest is evicted first.  Records survive character relogin until
they expire or are pushed out of the vendor's buyback list.
"""

import copy
import threading


class VendorBuybackLedger:
    """Vendor buyback ledger.

    Each (vendor_id, character_id) keeps an ordered list of at most slots records;
    the oldest is evicted first.  sell takes a list of item dicts with uid, price,
    level_req and optional bound, upgrade and enchant, and is all-or-nothing:
    bound items or a duplicate active uid refuse the whole batch.  Records carry
    record_id, uid, item snapshot, price and sold_at.  buyback finds the record for
    that vendor and character, then checks expiry (now - sold_at >= ttl),
    bag_free > 0, level >= level_req and gold >= price in that order; failures
    leave the record and gold unchanged.  A successful buyback deducts exactly the
    recorded sell price, returns status bought with charge, gold and the item
    snapshot, and removes the record.
    expire(now) removes expired records and returns their ids.  refresh_vendor does
    not touch buyback records.  slots and history return copies.  Record ids are
    unique across sells and buybacks.  Sell refusals use status refused with
    reason 'bound item' or 'duplicate item'; buyback statuses are bought, missing,
    expired, no_space, level_too_low and insufficient_funds.
    """

    def __init__(self, slots=5, ttl=3600.0):
        self._slot_limit = slots
        self._ttl = ttl
        self._gold = {}
        self._vendor_prices = {}
        self._records = {}
        self._active_uids = {}
        self._history = {}
        self._next_record_number = 1
        self._lock = threading.RLock()

    def set_gold(self, character_id, amount):
        with self._lock:
            self._gold[character_id] = amount

    def gold_of(self, character_id):
        with self._lock:
            return self._gold.get(character_id, 0)

    def set_vendor_price(self, vendor_id, item_id, price):
        with self._lock:
            self._vendor_prices.setdefault(vendor_id, {})[item_id] = price

    def sell(self, vendor_id, character_id, items, now=0.0):
        items = list(items)

        with self._lock:
            for item in items:
                if item.get("bound"):
                    return {"status": "refused", "reason": "bound item"}

            batch_uids = set()
            for item in items:
                uid = item["uid"]
                if uid in batch_uids or uid in self._active_uids:
                    return {"status": "refused", "reason": "duplicate item"}
                batch_uids.add(uid)

            key = (vendor_id, character_id)
            records = self._records.setdefault(key, [])
            history = self._history.setdefault(character_id, [])
            created = []

            for item in items:
                record = {
                    "record_id": self._new_record_id(),
                    "uid": item["uid"],
                    "item": copy.deepcopy(item),
                    "price": item["price"],
                    "sold_at": now,
                }
                records.append(record)
                history.append(copy.deepcopy(record))
                self._active_uids[record["uid"]] = record["record_id"]
                created.append(copy.deepcopy(record))

            while len(records) > self._slot_limit:
                evicted = records.pop(0)
                self._active_uids.pop(evicted["uid"], None)

            return {"status": "sold", "records": created}

    def buyback(self, vendor_id, character_id, record_id, now=0.0, bag_free=1, level=1):
        with self._lock:
            key = (vendor_id, character_id)
            records = self._records.get(key, [])
            record = next(
                (
                    candidate
                    for candidate in records
                    if candidate["record_id"] == record_id
                ),
                None,
            )
            if record is None:
                return {"status": "missing"}

            if now - record["sold_at"] >= self._ttl:
                return {"status": "expired"}

            if bag_free <= 0:
                return {"status": "no_space"}

            required_level = record["item"].get("level_req", 1)
            if level < required_level:
                return {
                    "status": "level_too_low",
                    "required_level": required_level,
                }

            price = record["price"]
            current_gold = self._gold.get(character_id, 0)
            if current_gold < price:
                return {"status": "insufficient_funds"}

            records.remove(record)
            self._active_uids.pop(record["uid"], None)
            self._gold[character_id] = current_gold - price

            return {
                "status": "bought",
                "charge": price,
                "gold": current_gold - price,
                "item": copy.deepcopy(record["item"]),
            }

    def expire(self, now):
        with self._lock:
            expired = []
            for records in self._records.values():
                remaining = []
                for record in records:
                    if now - record["sold_at"] >= self._ttl:
                        expired.append(record)
                        self._active_uids.pop(record["uid"], None)
                    else:
                        remaining.append(record)
                records[:] = remaining

            expired.sort(key=lambda record: self._record_number(record["record_id"]))
            return [record["record_id"] for record in expired]

    def refresh_vendor(self, vendor_id):
        with self._lock:
            return None

    def slots(self, vendor_id, character_id):
        with self._lock:
            return copy.deepcopy(self._records.get((vendor_id, character_id), []))

    def history_of(self, character_id):
        with self._lock:
            return copy.deepcopy(self._history.get(character_id, []))

    def _new_record_id(self):
        record_id = "S{}".format(self._next_record_number)
        self._next_record_number += 1
        return record_id

    @staticmethod
    def _record_number(record_id):
        try:
            return int(record_id[1:])
        except (TypeError, ValueError):
            return 0
