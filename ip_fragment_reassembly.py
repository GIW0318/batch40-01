"""IPv4/IPv6 fragment reassembly with overlap and resource policy."""


class IpFragmentReassembler:
    """Reassembles datagrams from fragments and enforces queue limits."""

    def __init__(self, timeout=10.0, max_datagrams=64, max_fragments=64,
                 max_bytes=65536, per_source_limit=4):
        raise NotImplementedError()

    def add(self, fragment, now):
        raise NotImplementedError()

    def tick(self, now):
        raise NotImplementedError()

    def needs_fragmentation(self, src, dst, mtu, now):
        raise NotImplementedError()

    def stats(self):
        raise NotImplementedError()
