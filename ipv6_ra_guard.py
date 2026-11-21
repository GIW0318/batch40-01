class RaPolicyGuard:
    """IPv6 router advertisement policy guard with per-source state."""

    def __init__(self, trusted_ports=None, dhcpv6_mode="stateful",
                 rate_limit=3, rate_window=1, max_preference="medium"):
        self.trusted_ports = set(trusted_ports or ())
        self.dhcpv6_mode = dhcpv6_mode
        self.rate_limit = rate_limit
        self.rate_window = rate_window
        self.max_preference = max_preference

    def inspect(self, ra, now=0):
        raise NotImplementedError

    def neighbor_advertisement(self, na):
        raise NotImplementedError

    def default_routers(self):
        raise NotImplementedError

    def prefixes(self):
        raise NotImplementedError

    def notices(self):
        raise NotImplementedError
