import socket
import struct

import pytest

import pcap_stream_reassembly as psr


def tcp_header(sport=1234, dport=80, seq=7, payload=b"", flags=0x10, urgent=0):
    return struct.pack(
        "!HHIIBBHHH", sport, dport, seq, 0, 5 << 4, flags, 65535, urgent, 0
    ) + payload


def ipv4(payload, ident=1, flags_frag=0, src="10.0.0.1", dst="10.0.0.2"):
    total_length = 20 + len(payload)
    return struct.pack(
        "!BBHHHBBH4s4s",
        0x45, 0, total_length, ident, flags_frag, 64, 6, 0,
        socket.inet_aton(src), socket.inet_aton(dst),
    ) + payload


def ethernet(payload, tags=()):
    frame = b"\x00" * 6 + b"\x11" * 6
    for index, tag in enumerate(tags):
        ethertype = 0x88A8 if len(tags) > 1 and index == 0 else 0x8100
        frame += struct.pack("!H", ethertype) + struct.pack("!H", tag)
    frame += struct.pack("!H", 0x0800) + payload
    return frame


def test_overlap_policy_is_first_wins_and_deterministic():
    def build():
        rebuilder = psr.TcpStreamRebuilder()
        rebuilder.add_segment("f", 0, b"AAAA")
        rebuilder.add_segment("f", 2, b"BBBB")
        return rebuilder.read("f", 0.0)

    first = build()
    second = build()
    assert first["data"] == b"AAAABB"
    assert first["policy"] == "first-wins"
    assert first == second


def test_sequence_wraparound_sorts_correctly():
    rebuilder = psr.TcpStreamRebuilder()
    rebuilder.add_segment("f", 0xFFFFFFF8, b"abcdefgh")
    rebuilder.add_segment("f", 0, b"ijklmnop")
    result = rebuilder.read("f", 0.0)
    assert result["data"] == b"abcdefghijklmnop"
    assert rebuilder.stats()["unique_bytes"] == 16


def test_retransmission_is_deduplicated():
    rebuilder = psr.TcpStreamRebuilder()
    rebuilder.add_segment("f", 0, b"0123456789")
    rebuilder.add_segment("f", 0, b"0123456789")
    stats = rebuilder.stats()
    assert rebuilder.read("f", 0.0)["data"] == b"0123456789"
    assert stats["segments"] == 2
    assert stats["bytes"] == 20
    assert stats["unique_bytes"] == 10


def test_out_of_order_segments_are_buffered():
    rebuilder = psr.TcpStreamRebuilder()
    rebuilder.add_segment("f", 100, b"b" * 100)
    rebuilder.add_segment("f", 0, b"a" * 100)
    assert rebuilder.read("f", 0.0)["data"] == b"a" * 100 + b"b" * 100


def test_gap_timeout_marks_gap_instead_of_waiting_forever():
    rebuilder = psr.TcpStreamRebuilder(gap_timeout=1.0)
    rebuilder.add_segment("f", 0, b"0123456789")
    rebuilder.add_segment("f", 20, b"abcdefghij")
    waiting = rebuilder.read("f", 0.5)
    expired = rebuilder.read("f", 1.5)
    assert waiting["data"] == b"0123456789"
    assert waiting["gaps"] == []
    assert expired["data"] == b"0123456789"
    assert expired["gaps"] == [{"start": 10, "end": 20}]


def test_fin_consumes_one_sequence_number():
    rebuilder = psr.TcpStreamRebuilder()
    rebuilder.add_segment("f", 0, b"x" * 10, flags="FIN")
    info = rebuilder.flow_info("f")
    assert info["fin_offset"] == 10
    assert rebuilder.read("f", 0.0)["data"] == b"x" * 10


def test_urgent_pointer_is_kept_separate():
    rebuilder = psr.TcpStreamRebuilder()
    rebuilder.add_segment("f", 0, b"abcdef", urgent=3)
    assert rebuilder.flow_info("f")["urgent"] == 3
    assert rebuilder.read("f", 0.0)["data"] == b"abcdef"


def test_per_flow_memory_limit_is_enforced():
    rebuilder = psr.TcpStreamRebuilder(per_flow_limit=16, global_limit=100)
    rebuilder.add_segment("f", 0, b"x" * 10)
    with pytest.raises(ValueError):
        rebuilder.add_segment("f", 10, b"y" * 10)
    assert len(rebuilder.read("f", 0.0)["data"]) == 10


def test_global_memory_limit_is_enforced():
    rebuilder = psr.TcpStreamRebuilder(per_flow_limit=32, global_limit=16)
    rebuilder.add_segment("f1", 0, b"x" * 10)
    with pytest.raises(ValueError):
        rebuilder.add_segment("f2", 0, b"y" * 10)
    assert rebuilder.stats()["flows"] == 1
    with pytest.raises(ValueError):
        rebuilder.read("f2", 0.0)


def test_directions_are_separate_streams():
    rebuilder = psr.TcpStreamRebuilder()
    rebuilder.add_segment("A>B", 0, b"request")
    rebuilder.add_segment("B>A", 0, b"response")
    assert rebuilder.read("A>B", 0.0)["data"] == b"request"
    assert rebuilder.read("B>A", 0.0)["data"] == b"response"
    assert rebuilder.stats()["flows"] == 2


def test_single_vlan_header_is_skipped():
    frame = ethernet(ipv4(tcp_header(payload=b"hello")), tags=[100])
    parsed = psr.TcpStreamRebuilder().parse_packet(frame)
    assert parsed["sport"] == 1234
    assert parsed["dport"] == 80
    assert parsed["payload"] == b"hello"
    assert parsed["vlan_tags"] == [100]


def test_qinq_double_vlan_is_skipped():
    frame = ethernet(ipv4(tcp_header(payload=b"world")), tags=[100, 200])
    parsed = psr.TcpStreamRebuilder().parse_packet(frame)
    assert parsed["payload"] == b"world"
    assert parsed["vlan_tags"] == [100, 200]


def test_ip_fragments_are_reassembled_before_tcp():
    rebuilder = psr.TcpStreamRebuilder()
    full_tcp = tcp_header(payload=b"abcdefgh")
    first = ethernet(ipv4(full_tcp[:24], ident=7, flags_frag=0x2000))
    second = ethernet(ipv4(full_tcp[24:], ident=7, flags_frag=0x0003))
    assert rebuilder.parse_packet(second) is None
    parsed = rebuilder.parse_packet(first)
    assert parsed["payload"] == b"abcdefgh"


def test_eviction_uses_monotonic_arrival_not_capture_timestamp():
    rebuilder = psr.TcpStreamRebuilder()
    rebuilder.add_segment("old", 0, b"x", ts=100, now=0.0)
    rebuilder.add_segment("new", 0, b"y", ts=40, now=9.0)
    assert rebuilder.evict(10.0, max_idle=5.0) == ["old"]
    assert rebuilder.stats()["flows"] == 1
    with pytest.raises(ValueError):
        rebuilder.flow_info("old")
    assert rebuilder.read("new", 0.0)["data"] == b"y"


def test_output_is_reproducible_for_different_insertion_order():
    segments = [(0, b"aa"), (2, b"bb"), (4, b"cc")]

    def build(items):
        rebuilder = psr.TcpStreamRebuilder()
        for seq, payload in items:
            rebuilder.add_segment("f", seq, payload)
        return rebuilder.read("f", 0.0)["data"]

    assert build(segments) == b"aabbcc"
    assert build(list(reversed(segments))) == b"aabbcc"
