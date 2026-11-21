"""Split tunnel routing, split DNS and fail-closed policy engine."""


class SplitTunnelPolicy:
    """Computes tunnel routes and DNS answers for a split tunnel client."""

    def __init__(self, tunnel_interface="tun0", physical_interface="eth0"):
        raise NotImplementedError()

    def load_policy(self, policy, now=0):
        raise NotImplementedError()

    def apply(self, now=0):
        raise NotImplementedError()

    def learn_dhcp_route(self, interface, cidr, metric):
        raise NotImplementedError()

    def decide(self, destination, app=None, now=0):
        raise NotImplementedError()

    def resolve(self, name, interface, now=0):
        raise NotImplementedError()

    def switch_interface(self, from_interface, to_interface, now=0):
        raise NotImplementedError()

    def complete_switch(self, now=0):
        raise NotImplementedError()

    def reconnect(self, now=0):
        raise NotImplementedError()

    def routes(self):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
