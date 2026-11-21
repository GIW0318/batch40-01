class DhcpLeaseBroker:
    """DHCP lease broker with reservations, option 82 and persistence."""

    def __init__(self, pools=None, reservations=None, min_lease=60,
                 max_lease=86400, quarantine=300):
        self.pools = list(pools or [])
        self.reservations = dict(reservations or {})
        self.min_lease = min_lease
        self.max_lease = max_lease
        self.quarantine = quarantine

    def normalize_mac(self, mac):
        raise NotImplementedError

    def offer(self, request, now=0):
        raise NotImplementedError

    def request(self, request, now=0):
        raise NotImplementedError

    def decline(self, request, now=0):
        raise NotImplementedError

    def release(self, request, now=0):
        raise NotImplementedError

    def expire(self, now=0):
        raise NotImplementedError

    def leases(self):
        raise NotImplementedError

    def dump_state(self):
        raise NotImplementedError

    def load_state(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
