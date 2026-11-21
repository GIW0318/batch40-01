import struct
import zlib

import ws_frame_audit as wfa


MASK = b"\x01\x02\x03\x04"


def frame(opcode, payload=b"", fin=True, mask=True, rsv=0, force_len=None):
    first = (0x80 if fin else 0x00) | (rsv << 4) | opcode
    if force_len is not None:
        header = bytes([first, (0x80 if mask else 0x00) | 127])
        header += struct.pack("!Q", force_len)
    else:
        length = len(payload)
        if length < 126:
            header = bytes([first, (0x80 if mask else 0x00) | length])
        elif length < 65536:
            header = bytes([first, (0x80 if mask else 0x00) | 126])
            header += struct.pack("!H", length)
        else:
            header = bytes([first, (0x80 if mask else 0x00) | 127])
            header += struct.pack("!Q", length)
    if mask:
        body = bytes(byte ^ MASK[i % 4] for i, byte in enumerate(payload))
        return header + MASK + body
    return header + payload


def close_frame(code, mask=True):
    return frame(8, struct.pack("!H", code), mask=mask)


def last_record(result):
    return result["records"][-1]


def test_interleaved_ping_keeps_fragments():
    auditor = wfa.WsFrameAudit()
    first = auditor.feed(1, frame(1, b"hel", fin=False))
    assert first["messages"] == []
    pinged = auditor.feed(1, frame(9, b"x"))
    assert pinged["out"][0] == 0x8A
    final = auditor.feed(1, frame(0, b"lo", fin=True))
    assert final["messages"] == ["hello"]


def test_control_frame_payload_over_125_is_rejected():
    auditor = wfa.WsFrameAudit()
    result = auditor.feed(1, frame(9, b"x" * 126))
    record = last_record(result)
    assert record["verdict"] == "reject"
    assert record["reason"] == "control_frame_too_long"
    assert auditor.state(1) == "closed"


def test_fragmented_control_frame_is_rejected():
    auditor = wfa.WsFrameAudit()
    record = last_record(auditor.feed(1, frame(9, b"x", fin=False)))
    assert record["verdict"] == "reject"
    assert record["reason"] == "fragmented_control"


def test_unmasked_client_frame_is_rejected():
    auditor = wfa.WsFrameAudit()
    record = last_record(auditor.feed(1, frame(1, b"hi", mask=False)))
    assert record["verdict"] == "reject"
    assert record["reason"] == "mask_required"


def test_masked_server_frame_is_rejected():
    auditor = wfa.WsFrameAudit()
    record = last_record(
        auditor.feed(1, frame(1, b"hi", mask=True), direction="server")
    )
    assert record["verdict"] == "reject"
    assert record["reason"] == "mask_forbidden"


def test_invalid_utf8_text_is_rejected():
    auditor = wfa.WsFrameAudit()
    record = last_record(auditor.feed(1, frame(1, b"\xff")))
    assert record["verdict"] == "reject"
    assert record["reason"] == "invalid_utf8"


def test_reserved_close_codes_are_rejected():
    auditor = wfa.WsFrameAudit()
    first = last_record(auditor.feed(1, close_frame(1005)))
    assert first["verdict"] == "reject"
    assert first["reason"] == "reserved_close_code"
    second = last_record(auditor.feed(2, close_frame(1006)))
    assert second["verdict"] == "reject"
    assert second["reason"] == "reserved_close_code"


def test_64bit_length_with_high_bit_is_rejected():
    auditor = wfa.WsFrameAudit()
    result = auditor.feed(1, frame(1, b"", force_len=1 << 63))
    record = last_record(result)
    assert record["verdict"] == "reject"
    assert record["reason"] == "invalid_64bit_length"


def test_continuation_without_start_is_rejected():
    auditor = wfa.WsFrameAudit()
    record = last_record(auditor.feed(1, frame(0, b"orphan")))
    assert record["verdict"] == "reject"
    assert record["reason"] == "unexpected_continuation"


def test_rsv_bit_without_extension_is_rejected():
    auditor = wfa.WsFrameAudit()
    record = last_record(auditor.feed(1, frame(1, b"hi", rsv=4)))
    assert record["verdict"] == "reject"
    assert record["reason"] == "rsv_without_extension"


def test_message_limit_applies_to_total_fragments():
    auditor = wfa.WsFrameAudit(max_message=100)
    assert auditor.feed(1, frame(1, b"a" * 10, fin=False))["messages"] == []
    for _ in range(8):
        result = auditor.feed(1, frame(0, b"a" * 10, fin=False))
        assert result["messages"] == []
    overflow = auditor.feed(1, frame(0, b"a" * 20, fin=True))
    record = last_record(overflow)
    assert record["verdict"] == "reject"
    assert record["reason"] == "message_too_large"


def test_compression_context_takeover_across_fragments():
    auditor = wfa.WsFrameAudit(
        extensions=("permessage-deflate",), compression=True
    )
    compressor = zlib.compressobj(wbits=-15)
    first_payload = compressor.compress(b"hello")
    first_payload += compressor.flush(zlib.Z_SYNC_FLUSH)
    first = auditor.feed(1, frame(1, first_payload, fin=False, rsv=4))
    assert first["messages"] == []
    second_payload = compressor.compress(b" world")
    second_payload += compressor.flush(zlib.Z_SYNC_FLUSH)
    second = auditor.feed(1, frame(0, second_payload, fin=True))
    assert second["messages"] == ["hello world"]


def test_close_handshake_is_two_way():
    auditor = wfa.WsFrameAudit()
    result = auditor.feed(1, close_frame(1000))
    assert result["out"] is not None
    assert result["out"][0] == 0x88
    assert auditor.state(1) == "closing"
    auditor.feed(1, close_frame(1000))
    assert auditor.state(1) == "closed"


def test_incremental_feed_parses_byte_by_byte():
    auditor = wfa.WsFrameAudit()
    data = frame(1, b"hey")
    result = None
    for index in range(len(data)):
        result = auditor.feed(1, data[index:index + 1])
    assert result["messages"] == ["hey"]
    assert auditor.audit()[-1]["length"] == 3


def test_audit_record_has_connection_type_length_and_verdict():
    auditor = wfa.WsFrameAudit()
    auditor.feed(7, frame(1, b"hi"))
    record = auditor.audit()[-1]
    assert record["conn"] == 7
    assert record["type"] == "text"
    assert record["length"] == 2
    assert record["verdict"] == "ok"
