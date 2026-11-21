import pytest

import netflow_aggregator as nfa


def flow_record(exporter="e1", seq=1, src="10.0.0.1", sport=1234,
                dst="10.0.0.2", dport=80, proto="tcp", byte_count=100,
                packets=1, t=0, flags="", icmp_type=None,
                icmp_code=None, flow_label=None):
    return {
        "exporter": exporter,
        "seq": seq,
        "src": src,
        "sport": sport,
        "dst": dst,
        "dport": dport,
        "proto": proto,
        "bytes": byte_count,
        "packets": packets,
        "t": t,
        "flags": flags,
        "icmp_type": icmp_type,
        "icmp_code": icmp_code,
        "flow_label": flow_label,
    }


def rollup(**kwargs):
    return nfa.FlowRollup(**kwargs)


def total_bytes(instance):
    return sum(flow["bytes"] for flow in instance.flows())


def test_bidirectional_records_merge_into_one_flow():
    engine = rollup()
    engine.ingest(flow_record(seq=1, byte_count=100))
    engine.ingest(flow_record(seq=2, src="10.0.0.2", sport=80,
                              dst="10.0.0.1", dport=1234, byte_count=40))
    flows = engine.flows()
    assert len(flows) == 1
    assert flows[0]["bytes"] == 140


def test_different_client_port_is_a_new_flow():
    engine = rollup()
    engine.ingest(flow_record(seq=1, sport=1234))
    engine.ingest(flow_record(seq=2, sport=1235))
    assert len(engine.flows()) == 2


def test_counter_wrap_is_accumulated_as_delta():
    engine = rollup()
    start = 4294967000
    engine.ingest(flow_record(seq=1, byte_count=start))
    engine.ingest(flow_record(seq=2, byte_count=100))
    expected = start + ((100 - start) % (2 ** 32))
    flows = engine.flows()
    assert len(flows) == 1
    assert flows[0]["bytes"] == expected


def test_idle_timeout_expires_flow():
    engine = rollup(idle_timeout=15)
    engine.ingest(flow_record(t=0))
    assert engine.expire(16) != []
    assert engine.expire(17) == []
    assert engine.flows()[0]["closed"] is True


def test_active_timeout_expires_long_lived_flow():
    engine = rollup(active_timeout=300, idle_timeout=15)
    for index, moment in enumerate((0, 100, 200, 300), start=1):
        engine.ingest(flow_record(seq=index, byte_count=index * 100, t=moment))
    assert engine.expire(301) != []
    assert engine.snapshot()["active"] == 0


def test_sampling_is_applied_only_once():
    engine = rollup(sampling_rate=100)
    engine.ingest(flow_record(byte_count=1000, packets=2, t=0))
    assert engine.flows()[0]["bytes"] == 100000
    assert engine.rollup(0)["bytes"] == 100000


def test_duplicate_exporter_sequence_is_ignored():
    engine = rollup()
    record = flow_record(seq=1, byte_count=100)
    assert engine.ingest(record) is True
    assert engine.ingest(record) is False
    assert len(engine.flows()) == 1
    assert engine.flows()[0]["bytes"] == 100


def test_lower_sequence_does_not_overwrite_newer_counter():
    engine = rollup()
    assert engine.ingest(flow_record(seq=2, byte_count=200)) is True
    assert engine.ingest(flow_record(seq=1, byte_count=100)) is False
    assert engine.flows()[0]["bytes"] == 200


def test_fin_closes_flow_and_later_data_opens_new_flow():
    engine = rollup()
    engine.ingest(flow_record(seq=1, byte_count=100, flags="FIN"))
    engine.ingest(flow_record(seq=2, byte_count=10))
    flows = engine.flows()
    assert len(flows) == 2
    assert sum(flow["bytes"] for flow in flows) == 110
    assert any(flow["closed"] for flow in flows)


def test_icmp_type_and_code_are_part_of_the_key():
    engine = rollup()
    engine.ingest(flow_record(seq=1, sport=0, dport=0, proto="icmp",
                              icmp_type=3, icmp_code=1))
    engine.ingest(flow_record(seq=2, sport=0, dport=0, proto="icmp",
                              icmp_type=3, icmp_code=3))
    assert len(engine.flows()) == 2


def test_ipv6_flow_label_is_part_of_the_key():
    engine = rollup()
    engine.ingest(flow_record(seq=1, proto="udp", flow_label=1))
    engine.ingest(flow_record(seq=2, proto="udp", flow_label=2))
    assert len(engine.flows()) == 2


def test_time_buckets_align_to_fixed_boundaries():
    engine = rollup(bucket_seconds=300)
    engine.ingest(flow_record(seq=1, sport=1000, byte_count=100, t=299))
    engine.ingest(flow_record(seq=2, sport=1001, byte_count=50, t=301))
    assert engine.rollup(0)["bytes"] == 100
    assert engine.rollup(300)["bytes"] == 50


def test_top_reports_sampling_error_margin():
    engine = rollup(sampling_rate=100)
    engine.ingest(flow_record(byte_count=1000, packets=4, t=0))
    entry = engine.top(1)[0]
    assert entry["bytes"] == 100000
    assert entry["error_margin_pct"] == pytest.approx(10.0)


def test_top_orders_flows_by_estimated_bytes():
    engine = rollup()
    engine.ingest(flow_record(seq=1, sport=1000, byte_count=500))
    engine.ingest(flow_record(seq=2, sport=1001, byte_count=1500))
    assert [entry["bytes"] for entry in engine.top(2)] == [1500, 500]


def test_retention_purges_old_closed_flows():
    engine = rollup(retention=3600)
    engine.ingest(flow_record(t=0, flags="RST"))
    assert engine.purge(100) == 0
    assert engine.purge(3601) == 1
    assert engine.flows() == []
