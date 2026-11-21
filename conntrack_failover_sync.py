"""Connection tracking state synchronization for active/standby failover."""


class ConntrackSyncAgent:
    """Keeps a conntrack table that can be exported, applied and reconciled."""

    def __init__(self, role="primary", timeout=120, table_limit=1000):
        raise NotImplementedError()

    def track(self, flow, now=0):
        raise NotImplementedError()

    def lookup(self, proto, src, dst, now=0):
        raise NotImplementedError()

    def check_seq(self, proto, src, dst, direction, seq, now=0):
        raise NotImplementedError()

    def effective_window(self, proto, src, dst):
        raise NotImplementedError()

    def export_full(self):
        raise NotImplementedError()

    def export_incremental(self, since=0):
        raise NotImplementedError()

    def apply(self, entries, now=0):
        raise NotImplementedError()

    def tick(self, now):
        raise NotImplementedError()

    def link_down(self, now=0):
        raise NotImplementedError()

    def resync(self, peer_entries, now=0):
        raise NotImplementedError()

    def diff(self, peer_entries):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
