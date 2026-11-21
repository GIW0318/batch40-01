import threading

import nat_binding_table as nbt


def make_table(**kw):
    kw.setdefault("external_ip", "203.0.113.1")
    kw.setdefault("tcp_timeout", 7200)
    kw.setdefault("udp_timeout", 60)
    kw.setdefault("time_wait", 30)
    kw.setdefault("capacity", 64)
    return nbt.NatBindingTable(**kw)


def packet(src, sport, dst, dport, protocol="tcp"):
    return {
        "src": src,
        "sport": sport,
        "dst": dst,
        "dport": dport,
        "protocol": protocol,
    }


def test_repeat_allocation_reuses_existing_binding():
    table = make_table()
    first = table.allocate("10.0.0.5", 12345, "tcp", now=0)
    second = table.allocate("10.0.0.5", 12345, "tcp", now=5)
    assert first["external_port"] == second["external_port"]
    assert second["created_at"] == 0
    assert table.stats()["bindings"] == 1


def test_concurrent_allocations_never_share_external_port():
    table = make_table(capacity=128)
    results = []
    errors = []
    barrier = threading.Barrier(4)

    def worker(base):
        try:
            barrier.wait()
            for i in range(10):
                binding = table.allocate(
                    "10.0.%d.%d" % (base, i), 1000 + i, "tcp", now=0
                )
                results.append(binding)
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 40
    assert len({item["external_port"] for item in results}) == 40
    assert table.stats()["bindings"] == 40


def test_time_wait_blocks_immediate_port_reuse():
    table = make_table(time_wait=30)
    first = table.allocate("10.0.0.5", 12345, "tcp", now=0)
    table.release("10.0.0.5", 12345, "tcp", now=10)
    second = table.allocate("10.0.0.5", 12345, "tcp", now=20)
    assert second["external_port"] != first["external_port"]
    assert table.stats()["time_wait"] == 1
    assert table.stats()["bindings"] == 2


def test_time_wait_binding_is_removed_after_window():
    table = make_table(time_wait=30)
    table.allocate("10.0.0.5", 12345, "tcp", now=0)
    table.release("10.0.0.5", 12345, "tcp", now=10)
    assert table.stats()["time_wait"] == 1
    table.expire(now=39)
    assert table.stats()["time_wait"] == 1
    table.expire(now=41)
    assert table.stats()["time_wait"] == 0


def test_tcp_and_udp_use_separate_timeouts():
    table = make_table(tcp_timeout=1000, udp_timeout=50)
    table.allocate("10.0.0.5", 1111, "tcp", now=0)
    table.allocate("10.0.0.6", 2222, "udp", now=0)
    table.expire(now=100)
    assert table.lookup_outbound("10.0.0.5", 1111, "tcp", now=100) is not None
    assert table.lookup_outbound("10.0.0.6", 2222, "udp", now=100) is None


def test_udp_session_is_refreshed_by_traffic():
    table = make_table(udp_timeout=60)
    table.allocate("10.0.0.5", 5353, "udp", now=0)
    assert table.lookup_outbound("10.0.0.5", 5353, "udp", now=50) is not None
    table.expire(now=100)
    assert table.stats()["bindings"] == 1
    table.expire(now=111)
    assert table.stats()["bindings"] == 0


def test_expire_only_scans_due_bindings():
    table = make_table(udp_timeout=20, capacity=32)
    for i in range(9):
        table.allocate("10.0.1.%d" % i, 5000 + i, "udp", now=0)
        table.lookup_outbound("10.0.1.%d" % i, 5000 + i, "udp", now=15)
    table.allocate("10.0.2.9", 5009, "udp", now=0)
    result = table.expire(now=21)
    assert len(result["expired"]) == 1
    assert result["scanned"] == 1


def test_eviction_drops_idle_binding_not_recently_used():
    table = make_table(capacity=2, tcp_timeout=10000)
    table.allocate("10.0.0.1", 1001, "tcp", now=0)
    table.allocate("10.0.0.2", 1002, "tcp", now=50)
    assert table.lookup_outbound("10.0.0.1", 1001, "tcp", now=60) is not None
    table.allocate("10.0.0.3", 1003, "tcp", now=70)
    assert table.lookup_outbound("10.0.0.1", 1001, "tcp", now=70) is not None
    assert table.lookup_outbound("10.0.0.2", 1002, "tcp", now=70) is None


def test_outbound_translation_rewrites_source():
    table = make_table()
    binding = table.allocate("10.0.0.5", 12345, "tcp", now=0)
    out = table.translate_outbound(
        packet("10.0.0.5", 12345, "8.8.8.8", 443), now=1
    )
    assert out["src"] == "203.0.113.1"
    assert out["sport"] == binding["external_port"]
    assert out["dst"] == "8.8.8.8"
    assert out["dport"] == 443


def test_inbound_translation_reverses_binding_tuple():
    table = make_table()
    binding = table.allocate("10.0.0.5", 12345, "tcp", now=0)
    inbound = table.translate_inbound(
        packet("8.8.8.8", 443, "203.0.113.1", binding["external_port"]), now=1
    )
    assert inbound["src"] == "8.8.8.8"
    assert inbound["sport"] == 443
    assert inbound["dst"] == "10.0.0.5"
    assert inbound["dport"] == 12345
    assert (
        table.lookup_inbound(
            "8.8.8.8", 443, binding["external_port"], "tcp", now=1
        )
        is not None
    )


def test_hairpin_reaches_internal_peer_via_public_address():
    table = make_table()
    binding = table.allocate("10.0.0.5", 12345, "tcp", now=0)
    out = table.translate_outbound(
        packet("10.0.0.6", 40000, "203.0.113.1", binding["external_port"]), now=1
    )
    assert out["dst"] == "10.0.0.5"
    assert out["dport"] == 12345
    assert out["hairpin"] is True


def test_icmp_error_is_mapped_back_to_internal_host():
    table = make_table()
    table.allocate("10.0.0.5", 12345, "udp", now=0)
    icmp = {
        "type": 3,
        "code": 3,
        "inner": packet("10.0.0.5", 12345, "8.8.8.8", 53, "udp"),
    }
    back = table.translate_icmp_error(icmp, now=1)
    assert back["matched"] is True
    assert back["dst"] == "10.0.0.5"
    assert back["dport"] == 12345
    unknown = {
        "type": 3,
        "code": 3,
        "inner": packet("10.0.0.9", 9999, "8.8.8.8", 53, "udp"),
    }
    assert table.translate_icmp_error(unknown, now=1)["matched"] is False


def test_fragments_use_first_fragment_to_translate_rest():
    table = make_table()
    binding = table.allocate("10.0.0.5", 12345, "udp", now=0)
    first = {
        "id": 7,
        "offset": 0,
        "more": True,
        "src": "10.0.0.5",
        "sport": 12345,
        "dst": "8.8.8.8",
        "dport": 53,
        "protocol": "udp",
        "payload": b"aaaa",
    }
    second = {
        "id": 7,
        "offset": 8,
        "more": False,
        "src": "10.0.0.5",
        "sport": None,
        "dst": "8.8.8.8",
        "dport": None,
        "protocol": "udp",
        "payload": b"bbbb",
    }
    out = table.translate_datagram([second, first], now=1)
    assert [item["offset"] for item in out] == [0, 8]
    assert {item["sport"] for item in out} == {binding["external_port"]}
    assert {item["src"] for item in out} == {"203.0.113.1"}


def test_snapshot_is_a_consistent_copy():
    table = make_table()
    table.allocate("10.0.0.5", 12345, "tcp", now=0)
    table.allocate("10.0.0.6", 12346, "tcp", now=0)
    snap = table.snapshot()
    assert len(snap) == 2
    table.release("10.0.0.5", 12345, "tcp", now=5)
    assert len(table.snapshot()) == 2
    assert len(snap) == 2
    snap[0]["internal_ip"] = "9.9.9.9"
    assert "9.9.9.9" not in {item["internal_ip"] for item in table.snapshot()}


def test_ipv6_prefix_mapping_round_trips_variable_host_bits():
    table = make_table()
    internal = "2001:db8:1::abcd"
    mapped = table.map_ipv6(
        internal, "2001:db8:1::/64", "2001:db8:aaaa::/64"
    )
    assert mapped.startswith("2001:db8:aaaa:")
    assert (
        table.map_ipv6(
            mapped,
            "2001:db8:1::/64",
            "2001:db8:aaaa::/64",
            reverse=True,
        )
        == internal
    )
