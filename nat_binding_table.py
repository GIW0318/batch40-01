class NatBindingTable:
    """Outbound NAT binding table with per-protocol timeouts and snapshots."""

    def __init__(self, external_ip="198.51.100.1", capacity=1024,
                 tcp_timeout=7200, udp_timeout=60, time_wait=30):
        self.external_ip = external_ip
        self.capacity = capacity
        self.tcp_timeout = tcp_timeout
        self.udp_timeout = udp_timeout
        self.time_wait = time_wait

    def allocate(self, internal_ip, internal_port, protocol="tcp", now=0):
        raise NotImplementedError

    def lookup_outbound(self, internal_ip, internal_port, protocol="tcp", now=0):
        raise NotImplementedError

    def lookup_inbound(self, remote_ip, remote_port, external_port,
                       protocol="tcp", now=0):
        raise NotImplementedError

    def translate_outbound(self, packet, now=0):
        raise NotImplementedError

    def translate_inbound(self, packet, now=0):
        raise NotImplementedError

    def translate_datagram(self, fragments, now=0):
        raise NotImplementedError

    def translate_icmp_error(self, icmp, now=0):
        raise NotImplementedError

    def release(self, internal_ip, internal_port, protocol="tcp", now=0):
        raise NotImplementedError

    def expire(self, now=0):
        raise NotImplementedError

    def map_ipv6(self, address, internal_prefix, external_prefix, reverse=False):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
