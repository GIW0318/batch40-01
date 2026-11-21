"""market_listing_revision (initial skeleton)."""


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
        raise NotImplementedError()

    def list_item(self, listing_id, seller, item, price, quantity, now=0.0, ttl=100.0):
        raise NotImplementedError()

    def revise(self, listing_id, price, expected_version, now=0.0):
        raise NotImplementedError()

    def buy(self, listing_id, buyer, quantity, seen_version, now=0.0):
        raise NotImplementedError()

    def cancel(self, listing_id, now=0.0, seller_offline=False):
        raise NotImplementedError()

    def expire(self, now):
        raise NotImplementedError()

    def get_listing(self, listing_id):
        raise NotImplementedError()

    def history(self, listing_id):
        raise NotImplementedError()

    def settle(self, listing_id):
        raise NotImplementedError()

    def mails(self, listing_id):
        raise NotImplementedError()
