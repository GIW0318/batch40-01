import h2_flow_fairness as h2f


def test_equal_weight_streams_round_robin():
    scheduler = h2f.FlowFairness(
        connection_window=1000, max_streams_per_tenant=10
    )
    assert scheduler.admit("a", 1)["admitted"] is True
    assert scheduler.admit("b", 2)["admitted"] is True
    assert scheduler.enqueue(1, 100)["accepted"] is True
    assert scheduler.enqueue(2, 100)["accepted"] is True
    assert scheduler.schedule(20) == [(1, 10), (2, 10)]


def test_connection_window_update_is_immediate():
    scheduler = h2f.FlowFairness(connection_window=10)
    result = scheduler.on_window(None, 5, connection=True)
    assert result["connection_window"] == 15
    assert scheduler.stats()["connection_window"] == 15


def test_schedule_stops_at_connection_window():
    scheduler = h2f.FlowFairness(
        connection_window=25, max_streams_per_tenant=10
    )
    scheduler.admit("a", 1)
    scheduler.admit("b", 2)
    scheduler.enqueue(1, 100)
    scheduler.enqueue(2, 100)
    allocations = scheduler.schedule(100)
    assert sum(size for _, size in allocations) == 25
    assert scheduler.stats()["connection_window"] == 0


def test_reset_releases_queued_bytes_and_quota():
    scheduler = h2f.FlowFairness(tenant_quota=100)
    scheduler.admit("a", 1)
    scheduler.enqueue(1, 40)
    assert scheduler.stats()["tenants"]["a"]["charged"] == 40
    released = scheduler.reset(1)
    assert released["released"] == 40
    assert scheduler.stats()["tenants"]["a"]["charged"] == 0
    assert scheduler.admit("a", 2)["admitted"] is True
    assert scheduler.enqueue(2, 90)["accepted"] is True


def test_reset_cleans_closed_dependency():
    scheduler = h2f.FlowFairness()
    scheduler.admit("a", 1)
    scheduler.admit("a", 2, depends_on=1)
    scheduler.reset(1)
    dependencies = scheduler.stats()["dependencies"]
    assert 1 not in dependencies
    assert dependencies[2] is None


def test_reset_stream_is_not_scheduled():
    scheduler = h2f.FlowFairness()
    scheduler.admit("a", 1)
    scheduler.admit("a", 2)
    scheduler.enqueue(1, 50)
    scheduler.enqueue(2, 50)
    scheduler.reset(1)
    allocations = scheduler.schedule(50)
    assert all(stream_id != 1 for stream_id, _ in allocations)
    assert sum(size for _, size in allocations) == 50


def test_tenant_stream_limit_is_per_tenant():
    scheduler = h2f.FlowFairness(max_streams_per_tenant=2)
    assert scheduler.admit("a", 1)["admitted"] is True
    assert scheduler.admit("a", 2)["admitted"] is True
    blocked = scheduler.admit("a", 3)
    assert blocked["admitted"] is False
    assert blocked["reason"] == "stream_limit"
    assert scheduler.admit("b", 4)["admitted"] is True


def test_goaway_rejects_new_streams():
    scheduler = h2f.FlowFairness()
    assert scheduler.admit("a", 5)["admitted"] is True
    scheduler.goaway(5)
    blocked = scheduler.admit("a", 7)
    assert blocked["admitted"] is False
    assert blocked["reason"] == "goaway"


def test_continuation_counts_toward_header_budget():
    scheduler = h2f.FlowFairness(
        max_header_bytes=64, max_streams_per_tenant=10
    )
    blocked = scheduler.admit("a", 1, headers=b"x" * 50, continuations=2)
    assert blocked["admitted"] is False
    assert blocked["reason"] == "header_too_large"
    assert scheduler.admit("a", 2, headers=b"x" * 50)["admitted"] is True


def test_padding_counts_toward_tenant_quota():
    scheduler = h2f.FlowFairness(
        tenant_quota=20, max_buffered=1000, max_streams_per_tenant=10
    )
    scheduler.admit("a", 1)
    blocked = scheduler.enqueue(1, 10, padding=12)
    assert blocked["accepted"] is False
    assert blocked["reason"] == "tenant_quota"
    assert scheduler.stats()["tenants"]["a"]["charged"] == 0
    assert scheduler.enqueue(1, 10)["accepted"] is True
    assert scheduler.stats()["tenants"]["a"]["charged"] == 10


def test_tenant_byte_quota_is_enforced():
    scheduler = h2f.FlowFairness(
        tenant_quota=20, max_buffered=1000, max_streams_per_tenant=10
    )
    scheduler.admit("a", 1)
    assert scheduler.enqueue(1, 15)["accepted"] is True
    blocked = scheduler.enqueue(1, 10)
    assert blocked["accepted"] is False
    assert blocked["reason"] == "tenant_quota"


def test_backpressure_limits_tenant_buffer():
    scheduler = h2f.FlowFairness(
        tenant_quota=1000, max_buffered=20, max_streams_per_tenant=10
    )
    scheduler.admit("a", 1)
    assert scheduler.enqueue(1, 15)["accepted"] is True
    blocked = scheduler.enqueue(1, 10)
    assert blocked["accepted"] is False
    assert blocked["reason"] == "backpressure"


def test_schedule_is_fair_across_tenants_by_bytes():
    scheduler = h2f.FlowFairness(
        connection_window=1000,
        max_streams_per_tenant=10,
        tenant_quota=10000,
        max_buffered=10000,
    )
    scheduler.admit("a", 1)
    for stream_id in (2, 3, 4, 5):
        scheduler.admit("b", stream_id)
    scheduler.enqueue(1, 100)
    for stream_id in (2, 3, 4, 5):
        scheduler.enqueue(stream_id, 25)
    scheduler.schedule(100)
    tenants = scheduler.stats()["tenants"]
    assert tenants["a"]["served"] == 50
    assert tenants["b"]["served"] == 50


def test_schedule_respects_stream_window():
    scheduler = h2f.FlowFairness(
        connection_window=1000, stream_window=15, max_streams_per_tenant=10
    )
    scheduler.admit("a", 1)
    scheduler.enqueue(1, 100)
    allocations = scheduler.schedule(100)
    assert sum(size for _, size in allocations) == 15
    assert scheduler.stats()["streams"] == 1


def test_stats_reports_windows_tenants_and_dependencies():
    scheduler = h2f.FlowFairness(connection_window=100, stream_window=50)
    scheduler.admit("a", 1)
    scheduler.enqueue(1, 30)
    stats = scheduler.stats()
    assert stats["connection_window"] == 100
    assert stats["streams"] == 1
    assert stats["tenants"]["a"]["queued"] == 30
    assert stats["tenants"]["a"]["served"] == 0
    assert stats["dependencies"][1] is None
