class DohResolverClient:
    """DNS-over-HTTPS client with bootstrap, pinning, padding and cache."""

    def __init__(self, bootstrap=("198.51.100.53",), server_name="doh.example",
                 pin_spki=None, pad_block=128, ttl_cap=300,
                 allow_plaintext=False, bootstrap_ttl=3600,
                 bootstrap_resolver=None, transport=None):
        self.bootstrap = list(bootstrap)
        self.server_name = server_name
        self.pin_spki = pin_spki
        self.pad_block = pad_block
        self.ttl_cap = ttl_cap
        self.allow_plaintext = allow_plaintext
        self.bootstrap_ttl = bootstrap_ttl
        self.bootstrap_resolver = bootstrap_resolver
        self.transport = transport

    def resolve(self, name, rtype="A", dnssec=False, cd=False, now=0):
        raise NotImplementedError

    def bootstrap_address(self, now=0):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
