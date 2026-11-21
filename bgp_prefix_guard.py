class BgpPrefixGuard:
    """BGP prefix admission guard with ROA, loop, flap and next-hop checks."""

    def __init__(self, local_as, roas=None, max_prefixes=1000,
                 allow_default=False, require_roa=True, flap_half_life=300,
                 flap_suppress=2000, flap_reuse=500):
        self.local_as = local_as
        self.roas = list(roas or [])
        self.max_prefixes = max_prefixes
        self.allow_default = allow_default
        self.require_roa = require_roa
        self.flap_half_life = flap_half_life
        self.flap_suppress = flap_suppress
        self.flap_reuse = flap_reuse

    def parse_as(self, value):
        raise NotImplementedError

    def announce(self, route, next_hop_reachable=True, now=0):
        raise NotImplementedError

    def withdraw(self, prefix, now=0):
        raise NotImplementedError

    def session_reset(self):
        raise NotImplementedError

    def best(self, prefix):
        raise NotImplementedError

    def rib(self):
        raise NotImplementedError

    def fib(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
