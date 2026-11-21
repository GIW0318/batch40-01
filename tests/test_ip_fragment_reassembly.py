import pytest

import ip_fragment_reassembly as ipfr


def frag(offset=0, mf=True, payload=b"", ident=1, src="10.0.0.1",
         dst="10.0.0.2", version=4, proto=6, next_header=None, df=False,
         total_length=None):
    data = {
        "version": version,
        "src": src,
        "dst": dst,
        "ident": ident,
        "proto": proto,
        "offset": offset,
        "mf": mf,
        "df": df,
        "payload": payload,
    }
    if next_header is not None:
        data["next_header"] = next_header
    if total_length is not None:
        data["total_length"] = total_length
    return data


def test_two_fragments_reassemble_in_order():
    asm = ipfr.IpFragmentReassembler()
    first = frag(offset=0, mf=True, payload=b"A" * 8, total_length=16)
    second = frag(offset=1, mf=False, payload=b"B" * 8)
    assert asm.add(first, 0) is None
    result = asm.add(second, 1)
    assert result["complete"] is True
    assert result["data"] == b"A" * 8 + b"B" * 8
    assert result["total_length"] == 16
    assert result["fragments"] == 2
    assert asm.stats()["completed"] == 1
    assert asm.stats()["active"] == 0


def test_out_of_order_fragments_assemble():
    asm = ipfr.IpFragmentReassembler()
    second = frag(offset=1, mf=False, payload=b"B" * 8)
    first = frag(offset=0, mf=True, payload=b"A" * 8, total_length=16)
    assert asm.add(second, 0) is None
    result = asm.add(first, 1)
    assert result["complete"] is True
    assert result["data"] == b"A" * 8 + b"B" * 8


def test_missing_middle_is_not_delivered_early():
    asm = ipfr.IpFragmentReassembler()
    first = frag(offset=0, mf=True, payload=b"A" * 8, total_length=24)
    last = frag(offset=2, mf=False, payload=b"C" * 8)
    middle = frag(offset=1, mf=True, payload=b"B" * 8)
    assert asm.add(first, 0) is None
    assert asm.add(last, 1) is None
    result = asm.add(middle, 2)
    assert result["complete"] is True
    assert result["data"] == b"A" * 8 + b"B" * 8 + b"C" * 8


def test_overlap_first_wins_in_both_arrival_orders():
    def assemble(order):
        asm = ipfr.IpFragmentReassembler()
        first = frag(offset=0, mf=True, payload=b"A" * 16, total_length=24)
        second = frag(offset=1, mf=False, payload=b"B" * 16)
        result = None
        for item in order:
            outcome = asm.add(item, 0)
            if outcome is not None:
                result = outcome
        return result

    expected = b"A" * 16 + b"B" * 8
    forward = assemble([
        frag(offset=0, mf=True, payload=b"A" * 16, total_length=24),
        frag(offset=1, mf=False, payload=b"B" * 16),
    ])
    backward = assemble([
        frag(offset=1, mf=False, payload=b"B" * 16),
        frag(offset=0, mf=True, payload=b"A" * 16, total_length=24),
    ])
    assert forward["data"] == expected
    assert backward["data"] == expected
    assert forward["overlaps"] == 8
    assert forward["policy"] == "first_wins"


def test_overlap_inside_small_first_fragment_does_not_duplicate():
    asm = ipfr.IpFragmentReassembler()
    first = frag(offset=0, mf=True, payload=b"A" * 8, total_length=16)
    second = frag(offset=0, mf=False, payload=b"B" * 16)
    assert asm.add(first, 0) is None
    result = asm.add(second, 1)
    assert result["data"] == b"A" * 8 + b"B" * 8
    assert result["total_length"] == 16
    assert result["overlaps"] == 8


def test_offset_overflow_is_rejected():
    asm = ipfr.IpFragmentReassembler()
    with pytest.raises(ValueError):
        asm.add(frag(offset=9000, mf=True, payload=b"X" * 8), 0)
    assert asm.stats()["rejected"] == 1
    assert asm.stats()["active"] == 0


def test_duplicate_fragment_does_not_inflate_total_length():
    asm = ipfr.IpFragmentReassembler()
    first = frag(offset=0, mf=True, payload=b"A" * 8, total_length=16)
    assert asm.add(first, 0) is None
    assert asm.add(dict(first), 1) is None
    result = asm.add(frag(offset=1, mf=False, payload=b"B" * 8), 2)
    assert result["complete"] is True
    assert result["total_length"] == 16
    assert result["fragments"] == 2
    assert result["duplicates"] == 1


def test_fragment_beyond_declared_total_is_rejected():
    asm = ipfr.IpFragmentReassembler()
    first = frag(offset=0, mf=True, payload=b"A" * 8, total_length=8)
    assert asm.add(first, 0) is None
    with pytest.raises(ValueError):
        asm.add(frag(offset=1, mf=False, payload=b"B" * 8), 1)
    assert asm.stats()["rejected"] == 1


def test_fragment_count_limit_is_enforced():
    asm = ipfr.IpFragmentReassembler(max_fragments=2)
    assert asm.add(frag(offset=0, mf=True, payload=b"A" * 8), 0) is None
    assert asm.add(frag(offset=1, mf=True, payload=b"B" * 8), 1) is None
    with pytest.raises(ValueError):
        asm.add(frag(offset=2, mf=False, payload=b"C" * 8), 2)
    assert asm.stats()["active"] == 1


def test_timed_out_queue_is_evicted():
    asm = ipfr.IpFragmentReassembler(timeout=10)
    assert asm.add(frag(offset=0, mf=True, payload=b"A" * 8), 0) is None
    assert asm.tick(11) == 1
    assert asm.stats()["dropped_timeout"] == 1
    assert asm.stats()["active"] == 0


def test_timeout_does_not_evict_fresh_queue():
    asm = ipfr.IpFragmentReassembler(timeout=10, per_source_limit=4)
    assert asm.add(frag(ident=1, offset=0, mf=True, payload=b"A" * 8), 0) is None
    assert asm.add(frag(ident=2, offset=0, mf=True, payload=b"B" * 8), 9) is None
    assert asm.tick(11) == 1
    assert asm.stats()["dropped_timeout"] == 1
    assert asm.stats()["active"] == 1


def test_per_source_quota_evicts_oldest_queue():
    asm = ipfr.IpFragmentReassembler(per_source_limit=2)
    assert asm.add(frag(ident=1, offset=0, mf=True, payload=b"A" * 8), 0) is None
    assert asm.add(frag(ident=2, offset=0, mf=True, payload=b"B" * 8), 1) is None
    assert asm.add(frag(ident=3, offset=0, mf=True, payload=b"C" * 8), 2) is None
    assert asm.stats()["dropped_quota"] == 1
    assert asm.stats()["active"] == 2


def test_ipv6_next_header_is_resolved_from_first_fragment():
    asm = ipfr.IpFragmentReassembler()
    first = frag(version=6, ident=9, next_header=17, proto=44, offset=0,
                 mf=True, payload=b"A" * 8, total_length=16)
    second = frag(version=6, ident=9, next_header=44, proto=44, offset=1,
                  mf=False, payload=b"B" * 8)
    assert asm.add(first, 0) is None
    result = asm.add(second, 1)
    assert result["complete"] is True
    assert result["proto"] == 17


def test_atomic_fragment_passes_through():
    asm = ipfr.IpFragmentReassembler()
    result = asm.add(frag(offset=0, mf=False, df=True, payload=b"mini"), 0)
    assert result["complete"] is True
    assert result["atomic"] is True
    assert result["data"] == b"mini"
    assert asm.stats()["atomic"] == 1
    assert asm.stats()["active"] == 0


def test_df_conflict_and_fragmentation_needed_signal():
    asm = ipfr.IpFragmentReassembler()
    with pytest.raises(ValueError):
        asm.add(frag(offset=0, mf=True, df=True, payload=b"X" * 8), 0)
    assert asm.stats()["rejected"] == 1
    signal = asm.needs_fragmentation("10.0.0.1", "10.0.0.2", 1400, 0)
    assert signal["action"] == "notify_upper"
    assert signal["mtu"] == 1400
    assert asm.stats()["needs_fragmentation"] == 1
