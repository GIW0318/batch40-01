class StapleGate:
    """OCSP stapling verification gate with chain-wide checks and cache."""

    def __init__(self, allow_soft_fail=True, max_staple_size=16384,
                 clock_skew=300, cache_ttl=3600, fetch_source=None):
        self.allow_soft_fail = allow_soft_fail
        self.max_staple_size = max_staple_size
        self.clock_skew = clock_skew
        self.cache_ttl = cache_ttl
        self.fetch_source = fetch_source

    def verify(self, chain, staples, nonce=None, now=0, fetch_error=None):
        raise NotImplementedError

    def fetch(self, chain, now=0):
        raise NotImplementedError

    def store(self, chain, staple, version=0):
        raise NotImplementedError

    def cache(self, chain):
        raise NotImplementedError

    def cache_key(self, chain):
        raise NotImplementedError

    def policy(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
