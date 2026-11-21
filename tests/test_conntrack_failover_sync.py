import pytest

import conntrack_failover_sync as cfs


def flow(proto="tcp", src=("10.0.0.1", 1234), dst=("10.0.0.2", 80),
         state="established", seq_local=1000, seq_remote=5000, window=1000,
         window_scale=7, nat_src=None, nat_dst=None, timeout=90, priority=3,
         related=None):
    return {
        "proto": proto,
        "src": src,
        "dst": dst,
        "state": state,
        "seq_local": seq_local,
        "seq_remote": seq_remote,
        "window": window,
        "window_scale": window_scale,
        "nat_src": nat_src,
        "nat_dst": nat_dst,
        "timeout": timeout,
        "priority": priority,
        "related": related,
    }


def agent(**kwargs):
    return cfs.ConntrackSyncAgent(**kwargs)


def key_of(entry):
    return tuple(entry["key"])


def test_full_export_carries_established_and_handshake_states():
    primary = agent(role="primary")
    primary.track(flow(state="syn_sent"), now=0)
    primary.track(flow(src=("10.0.0.3", 5555), dst=("10.0.0.4", 443),
                       state="established"), now=0)
    entries = primary.export_full()
    assert len(entries) == 2
    standby = agent(role="standby")
    applied = standby.apply(entries, now=0)
    assert applied["applied"] == 2
    assert standby.lookup("tcp", ("10.0.0.1", 1234), ("10.0.0.2", 80))["state"] == "syn_sent"
    assert standby.lookup("tcp", ("10.0.0.3", 5555), ("10.0.0.4", 443))["state"] == "established"


def test_incremental_export_only_returns_newer_versions():
    primary = agent()
    primary.track(flow(), now=0)
    primary.track(flow(src=("10.0.0.9", 1000), dst=("10.0.0.8", 80)), now=1)
    incremental = primary.export_incremental(1)
    assert len(incremental) == 1
    assert incremental[0]["version"] == 2
    assert len(primary.export_full()) == 2


def test_older_sync_entry_does_not_overwrite_newer_state():
    primary = agent()
    primary.track(flow(state="established"), now=0)
    entry = primary.export_full()[0]
    standby = agent(role="standby")
    assert standby.apply([entry], now=0)["applied"] == 1
    stale = dict(entry, version=0, state="closed")
    result = standby.apply([stale], now=1)
    assert result["ignored"] == 1
    assert standby.lookup("tcp", ("10.0.0.1", 1234), ("10.0.0.2", 80))["state"] == "established"


def test_newer_sync_entry_replaces_state():
    primary = agent()
    primary.track(flow(state="established"), now=0)
    entry = dict(primary.export_full()[0], version=5, state="syn_recv")
    standby = agent(role="standby")
    result = standby.apply([entry], now=0)
    assert result["applied"] == 1
    assert standby.lookup("tcp", ("10.0.0.1", 1234), ("10.0.0.2", 80))["state"] == "syn_recv"


def test_sequence_translation_restores_window_position():
    primary = agent()
    primary.track(flow(seq_local=1000, seq_remote=5000, window=1000), now=0)
    first = primary.check_seq("tcp", ("10.0.0.1", 1234), ("10.0.0.2", 80),
                              "out", 5001)
    second = primary.check_seq("tcp", ("10.0.0.1", 1234), ("10.0.0.2", 80),
                               "out", 5999)
    assert first["translated"] == 1001
    assert first["in_window"] is True
    assert second["translated"] == 1999
    assert second["in_window"] is True


def test_sequence_at_window_edge_is_out_of_window():
    primary = agent()
    primary.track(flow(seq_local=1000, seq_remote=5000, window=1000), now=0)
    edge = primary.check_seq("tcp", ("10.0.0.1", 1234), ("10.0.0.2", 80),
                             "out", 6000)
    behind = primary.check_seq("tcp", ("10.0.0.1", 1234), ("10.0.0.2", 80),
                               "out", 4999)
    assert edge["translated"] == 2000
    assert edge["in_window"] is False
    assert behind["translated"] == 999
    assert behind["in_window"] is False


def test_asymmetric_routing_falls_back_to_reverse_tuple():
    primary = agent()
    primary.track(flow(), now=0)
    reverse = primary.lookup("tcp", ("10.0.0.2", 80), ("10.0.0.1", 1234))
    assert reverse is not None
    assert reverse["matched"] == "reverse"
    assert reverse["state"] == "established"


def test_nat_tuple_resolves_to_original_connection():
    primary = agent()
    primary.track(flow(nat_src=("192.0.2.9", 40000),
                       nat_dst=("10.0.0.2", 80)), now=0)
    translated = primary.lookup("tcp", ("192.0.2.9", 40000), ("10.0.0.2", 80))
    assert translated is not None
    assert translated["matched"] == "nat"
    assert translated["src"] == ("10.0.0.1", 1234)


def test_local_timeout_prevents_premature_expiry():
    primary = agent(timeout=120)
    primary.track(flow(timeout=60), now=0)
    entries = primary.export_full()
    standby = agent(role="standby", timeout=300)
    standby.apply(entries, now=0)
    assert standby.tick(100) == 0
    assert standby.lookup("tcp", ("10.0.0.1", 1234), ("10.0.0.2", 80),
                          now=100) is not None
    assert standby.tick(301) == 1
    assert standby.lookup("tcp", ("10.0.0.1", 1234), ("10.0.0.2", 80),
                          now=301) is None


def test_failback_apply_is_idempotent():
    primary = agent()
    primary.track(flow(), now=0)
    entries = primary.export_full()
    standby = agent(role="standby")
    first = standby.apply(entries, now=0)
    second = standby.apply(entries, now=1)
    assert first["applied"] == 1
    assert second["applied"] == 0
    assert second["ignored"] == 1
    assert standby.snapshot()["count"] == 1


def test_link_down_resync_rebuilds_full_table():
    standby = agent(role="standby")
    standby.track(flow(src=("10.0.0.7", 7000), dst=("10.0.0.8", 80)), now=0)
    standby.link_down(now=1)
    assert standby.snapshot()["in_sync"] is False
    peer = agent()
    peer.track(flow(src=("10.0.0.9", 9000), dst=("10.0.0.10", 80)), now=0)
    result = standby.resync(peer.export_full(), now=2)
    assert result["rebuilt"] == 1
    assert result["removed"] == 1
    assert standby.lookup("tcp", ("10.0.0.7", 7000), ("10.0.0.8", 80)) is None
    assert standby.lookup("tcp", ("10.0.0.9", 9000), ("10.0.0.10", 80)) is not None
    assert standby.snapshot()["in_sync"] is True


def test_window_scale_travels_with_state():
    primary = agent()
    primary.track(flow(window=1000, window_scale=7), now=0)
    entry = primary.export_full()[0]
    assert entry["window_scale"] == 7
    standby = agent(role="standby")
    standby.apply([entry], now=0)
    assert standby.effective_window("tcp", ("10.0.0.1", 1234),
                                    ("10.0.0.2", 80)) == 128000


def test_icmp_related_entry_resolves_to_original_flow():
    primary = agent()
    original = primary.track(flow(), now=0)
    primary.track(flow(proto="icmp", src=("10.0.0.2", 0), dst=("10.0.0.1", 0),
                       state="related", related=original["key"]), now=1)
    standby = agent(role="standby")
    standby.apply(primary.export_full(), now=2)
    resolved = standby.lookup("icmp", ("10.0.0.2", 0), ("10.0.0.1", 0))
    assert resolved is not None
    assert resolved["via_related"] is True
    assert resolved["state"] == "established"


def test_capacity_pressure_evicts_lowest_priority():
    primary = agent(table_limit=2)
    primary.track(flow(state="syn_sent", priority=1), now=0)
    primary.track(flow(src=("10.0.0.3", 3000), dst=("10.0.0.4", 80),
                       state="established", priority=5), now=1)
    primary.track(flow(src=("10.0.0.5", 5000), dst=("10.0.0.6", 80),
                       state="established", priority=3), now=2)
    assert primary.snapshot()["count"] == 2
    assert primary.snapshot()["evicted_capacity"] == 1
    assert primary.lookup("tcp", ("10.0.0.1", 1234), ("10.0.0.2", 80)) is None
    assert primary.lookup("tcp", ("10.0.0.5", 5000), ("10.0.0.6", 80)) is not None


def test_diff_reports_peer_differences():
    local = agent()
    local.track(flow(src=("10.0.0.1", 1111), dst=("10.0.0.2", 80)), now=0)
    local.track(flow(src=("10.0.0.3", 3333), dst=("10.0.0.4", 80)), now=1)
    peer = agent(role="standby")
    peer.track(flow(src=("10.0.0.3", 3333), dst=("10.0.0.4", 80)), now=0)
    peer.track(flow(src=("10.0.0.5", 5555), dst=("10.0.0.6", 80)), now=1)
    diff = local.diff(peer.export_full())
    missing = {tuple(item) for item in diff["missing_on_peer"]}
    extra = {tuple(item) for item in diff["extra_on_peer"]}
    mismatched = {tuple(item) for item in diff["version_mismatch"]}
    assert ("tcp", "10.0.0.1", 1111, "10.0.0.2", 80) in missing
    assert ("tcp", "10.0.0.5", 5555, "10.0.0.6", 80) in extra
    assert ("tcp", "10.0.0.3", 3333, "10.0.0.4", 80) in mismatched
