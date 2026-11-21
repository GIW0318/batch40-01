class MtlsPrincipalMap:
    """Derive a service principal from a verified mutual-TLS certificate chain."""

    def __init__(self, trusted_proxies=None, trust_domains=None,
                 allow_cn_fallback=False, revocation_check_interval=300,
                 cache_ttl=60, known_critical_extensions=None):
        raise NotImplementedError()

    def add_root(self, cert):
        raise NotImplementedError()

    def add_trust_bundle(self, name, certs):
        raise NotImplementedError()

    def revoke(self, fingerprint, at=0):
        raise NotImplementedError()

    def canonical_uri(self, uri):
        raise NotImplementedError()

    def match_dns(self, pattern, hostname):
        raise NotImplementedError()

    def authorize_host(self, cert, hostname):
        raise NotImplementedError()

    def map_request(self, request, now=0):
        raise NotImplementedError()

    def revalidate(self, mapping, now):
        raise NotImplementedError()
