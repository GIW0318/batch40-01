"""vendor_buyback_ledger (initial skeleton)."""


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
        raise NotImplementedError()

    def set_gold(self, character_id, amount):
        raise NotImplementedError()

    def gold_of(self, character_id):
        raise NotImplementedError()

    def set_vendor_price(self, vendor_id, item_id, price):
        raise NotImplementedError()

    def sell(self, vendor_id, character_id, items, now=0.0):
        raise NotImplementedError()

    def buyback(self, vendor_id, character_id, record_id, now=0.0, bag_free=1, level=1):
        raise NotImplementedError()

    def expire(self, now):
        raise NotImplementedError()

    def refresh_vendor(self, vendor_id):
        raise NotImplementedError()

    def slots(self, vendor_id, character_id):
        raise NotImplementedError()

    def history_of(self, character_id):
        raise NotImplementedError()
