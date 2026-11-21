"""Optimistic-concurrency revision ledger for auction house listings."""

import copy


class MarketListingRevision:
    """Auction listing revision ledger.

    A listing starts at version 1 with listing_id, seller, item, price, quantity,
    status, created_at, ttl, last_revise and history.  revise requires
    expected_version (None is missing version) and appends a history row; stale
    versions, cooldown and max_revisions are refused.  buy requires seen_version to
    equal the current version and charges the price of that version:
    total = price * quantity, fee = total * fee_bps // 10000, seller_net =
    total - fee.  A full fill sets status sold; a partial fill marks the original
    partial and creates <listing_id>-r<N> with version + 1, the same price and the
    remaining quantity, continuing the history chain.  Sold, cancelled, expired and
    partial listings are unavailable.  A listing expires when
    now - created_at >= ttl.  cancel and expire are mutually exclusive
    and queue exactly one return mail.  get_listing, history, settle and mails
    return deep copies, and settlement rows carry listing_id and version.
    revise refusal reasons are 'missing version', 'stale version', 'cooldown'
    (with retry_at) and 'revision limit'; buy reports stale_version and returns
    sold or partial; inactive listings return unavailable; cancel returns
    cancelled or expired.
    """

    def __init__(self, fee_bps=500, revise_cooldown=0.0, max_revisions=None):
        self.fee_bps = fee_bps
        self.revise_cooldown = revise_cooldown
        self.max_revisions = max_revisions
        self._listings = {}
        self._revision_counts = {}
        self._remainder_seqs = {}
        self._sales = {}
        self._mails = {}

    def list_item(self, listing_id, seller, item, price, quantity, now=0.0, ttl=100.0):
        listing = {
            "listing_id": listing_id,
            "seller": seller,
            "item": copy.deepcopy(item),
            "price": price,
            "quantity": quantity,
            "status": "active",
            "created_at": now,
            "ttl": ttl,
            "last_revise": None,
            "version": 1,
            "history": [
                {
                    "version": 1,
                    "price": price,
                    "quantity": quantity,
                    "at": now,
                    "event": "list",
                }
            ],
        }
        self._listings[listing_id] = listing
        self._revision_counts[listing_id] = 0
        self._remainder_seqs[listing_id] = 0
        self._sales[listing_id] = []
        self._mails[listing_id] = []
        return copy.deepcopy(listing)

    def revise(self, listing_id, price, expected_version, now=0.0):
        listing = self._listings.get(listing_id)
        if listing is None or listing["status"] != "active":
            return {"revised": False, "reason": "unavailable"}
        if expected_version is None:
            return {"revised": False, "reason": "missing version"}
        if expected_version != listing["version"]:
            return {
                "revised": False,
                "reason": "stale version",
                "listing_version": listing["version"],
            }
        retry_at = None
        if listing["last_revise"] is not None:
            retry_at = listing["last_revise"] + self.revise_cooldown
        if retry_at is not None and now < retry_at:
            return {"revised": False, "reason": "cooldown", "retry_at": retry_at}
        if self.max_revisions is not None and self._revision_counts[listing_id] >= self.max_revisions:
            return {"revised": False, "reason": "revision limit"}
        listing["price"] = price
        listing["version"] += 1
        listing["last_revise"] = now
        self._revision_counts[listing_id] += 1
        listing["history"].append(
            {
                "version": listing["version"],
                "price": price,
                "quantity": listing["quantity"],
                "at": now,
                "event": "revise",
            }
        )
        return {
            "revised": True,
            "version": listing["version"],
            "price": price,
        }

    def buy(self, listing_id, buyer, quantity, seen_version, now=0.0):
        listing = self._listings.get(listing_id)
        if listing is None or listing["status"] != "active":
            return {"status": "unavailable", "listing_id": listing_id}
        if seen_version != listing["version"]:
            return {
                "status": "stale_version",
                "listing_id": listing_id,
                "listing_version": listing["version"],
            }
        if quantity <= 0 or quantity > listing["quantity"]:
            return {"status": "unavailable", "listing_id": listing_id}

        locked_version = listing["version"]
        locked_price = listing["price"]
        total = locked_price * quantity
        fee = total * self.fee_bps // 10000
        seller_net = total - fee
        sale = {
            "listing_id": listing_id,
            "version": locked_version,
            "buyer": buyer,
            "item": copy.deepcopy(listing["item"]),
            "quantity": quantity,
            "price": locked_price,
            "total": total,
            "fee": fee,
            "seller_net": seller_net,
            "at": now,
        }
        self._sales[listing_id].append(sale)

        if quantity >= listing["quantity"]:
            listing["quantity"] = 0
            listing["status"] = "sold"
            return {
                "status": "sold",
                "listing_id": listing_id,
                "version": locked_version,
                "quantity": quantity,
                "price": locked_price,
                "total": total,
                "fee": fee,
                "seller_net": seller_net,
            }

        remaining_quantity = listing["quantity"] - quantity
        listing["quantity"] = remaining_quantity
        listing["status"] = "partial"

        self._remainder_seqs[listing_id] += 1
        remaining_id = "{}-r{}".format(listing_id, self._remainder_seqs[listing_id])
        remaining = {
            "listing_id": remaining_id,
            "seller": listing["seller"],
            "item": copy.deepcopy(listing["item"]),
            "price": locked_price,
            "quantity": remaining_quantity,
            "status": "active",
            "created_at": listing["created_at"],
            "ttl": listing["ttl"],
            "last_revise": listing["last_revise"],
            "version": locked_version + 1,
            "history": copy.deepcopy(listing["history"])
            + [
                {
                    "version": locked_version + 1,
                    "price": locked_price,
                    "quantity": remaining_quantity,
                    "at": now,
                    "event": "partial",
                }
            ],
        }
        self._listings[remaining_id] = remaining
        self._revision_counts[remaining_id] = self._revision_counts[listing_id]
        self._remainder_seqs[remaining_id] = 0
        self._sales[remaining_id] = []
        self._mails[remaining_id] = []
        return {
            "status": "partial",
            "listing_id": listing_id,
            "remaining_listing_id": remaining_id,
            "version": locked_version,
            "quantity": quantity,
            "price": locked_price,
            "total": total,
            "fee": fee,
            "seller_net": seller_net,
        }

    def cancel(self, listing_id, now=0.0, seller_offline=False):
        listing = self._listings.get(listing_id)
        if listing is None:
            return {"status": "unavailable", "listing_id": listing_id}
        if listing["status"] == "expired":
            return {"status": "expired", "listing_id": listing_id}
        if listing["status"] != "active":
            return {"status": "unavailable", "listing_id": listing_id}
        listing["status"] = "cancelled"
        self._mails[listing_id].append(
            {
                "type": "return",
                "listing_id": listing_id,
                "version": listing["version"],
                "item": copy.deepcopy(listing["item"]),
                "quantity": listing["quantity"],
                "at": now,
                "seller_offline": seller_offline,
            }
        )
        return {
            "status": "cancelled",
            "listing_id": listing_id,
            "version": listing["version"],
        }

    def expire(self, now):
        expired_ids = []
        for listing_id, listing in self._listings.items():
            if listing["status"] != "active":
                continue
            if now - listing["created_at"] < listing["ttl"]:
                continue
            listing["status"] = "expired"
            self._mails[listing_id].append(
                {
                    "type": "return",
                    "listing_id": listing_id,
                    "version": listing["version"],
                    "item": copy.deepcopy(listing["item"]),
                    "quantity": listing["quantity"],
                    "at": now,
                    "seller_offline": False,
                }
            )
            expired_ids.append(listing_id)
        return expired_ids

    def get_listing(self, listing_id):
        listing = self._listings.get(listing_id)
        if listing is None:
            return None
        return copy.deepcopy(listing)

    def history(self, listing_id):
        listing = self._listings.get(listing_id)
        if listing is None:
            return []
        return copy.deepcopy(listing["history"])

    def settle(self, listing_id):
        return {
            "listing_id": listing_id,
            "sales": copy.deepcopy(self._sales.get(listing_id, [])),
        }

    def mails(self, listing_id):
        return copy.deepcopy(self._mails.get(listing_id, []))
