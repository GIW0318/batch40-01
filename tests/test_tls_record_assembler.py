import pytest

import tls_record_assembler as tra

CT_CCS = 20
CT_ALERT = 21
CT_HANDSHAKE = 22
CT_APPDATA = 23


def record(content_type, payload=b"", version=0x0303):
    return (bytes([content_type])
            + version.to_bytes(2, "big")
            + len(payload).to_bytes(2, "big")
            + payload)


def handshake(msg_type, body):
    return bytes([msg_type]) + len(body).to_bytes(3, "big") + body


def finished():
    return record(CT_HANDSHAKE, handshake(20, b"finished"))


def test_split_record_is_buffered_until_complete():
    asm = tra.TlsRecordAssembler()
    body = handshake(1, b"client-hello")
    data = record(CT_HANDSHAKE, body)
    assert asm.feed(data[:7]) == []
    assert asm.pending() == 7
    events = asm.feed(data[7:])
    assert len(events) == 1
    assert events[0]["content_type"] == CT_HANDSHAKE
    assert events[0]["payload_len"] == len(body)
    assert asm.pending() == 0


def test_three_records_in_one_read_are_all_parsed():
    asm = tra.TlsRecordAssembler()
    data = (record(CT_HANDSHAKE, handshake(1, b"ch"))
            + record(CT_HANDSHAKE, handshake(2, b"sh"))
            + record(CT_HANDSHAKE, handshake(11, b"cert")))
    events = asm.feed(data)
    assert len(events) == 3
    assert [item["handshake_type"] for item in events] == [1, 2, 11]
    assert asm.audit()["records_in"] == 3


def test_oversized_record_is_rejected_before_allocation():
    asm = tra.TlsRecordAssembler(max_record_len=1024)
    header = bytes([CT_HANDSHAKE, 0x03, 0x03]) + (2000).to_bytes(2, "big")
    with pytest.raises(tra.RecordLayerError) as info:
        asm.feed(header)
    assert info.value.kind == "record_overflow"
    assert info.value.fatal is True
    assert asm.errors()["record_overflow"] == 1
    assert asm.pending() == 0


def test_handshake_message_is_reassembled_across_records():
    asm = tra.TlsRecordAssembler()
    body = b"x" * 300
    full = handshake(1, body)
    first = asm.feed(record(CT_HANDSHAKE, full[:100]))
    assert len(first) == 1
    assert first[0]["handshake_complete"] is False
    second = asm.feed(record(CT_HANDSHAKE, full[100:]))
    assert second[0]["handshake_complete"] is True
    assert second[0]["handshake_type"] == 1
    assert second[0]["handshake_body"] == body


def test_tls13_change_cipher_spec_mid_handshake_is_rejected():
    asm = tra.TlsRecordAssembler(negotiated_version=0x0304)
    asm.feed(record(CT_HANDSHAKE, handshake(2, b"server-hello")))
    with pytest.raises(tra.RecordLayerError) as info:
        asm.feed(record(CT_CCS, b"\x01"))
    assert info.value.kind == "unexpected_change_cipher_spec"


def test_application_data_after_close_notify_is_rejected():
    asm = tra.TlsRecordAssembler()
    asm.feed(record(CT_HANDSHAKE, handshake(1, b"ch")))
    asm.feed(finished())
    alert = asm.feed(record(CT_ALERT, b"\x01\x00"))
    assert alert[0]["kind"] == "alert"
    assert alert[0]["close_notify"] is True
    with pytest.raises(tra.RecordLayerError) as info:
        asm.feed(record(CT_APPDATA, b"late"))
    assert info.value.kind == "application_data_after_close"


def test_application_data_before_handshake_complete_is_rejected():
    asm = tra.TlsRecordAssembler()
    asm.feed(record(CT_HANDSHAKE, handshake(1, b"ch")))
    with pytest.raises(tra.RecordLayerError) as info:
        asm.feed(record(CT_APPDATA, b"early"))
    assert info.value.kind == "handshake_incomplete"


def test_record_version_mismatch_is_warned():
    asm = tra.TlsRecordAssembler(negotiated_version=0x0303)
    events = asm.feed(record(CT_HANDSHAKE, handshake(1, b"ch"), version=0x0301))
    assert "record_version_mismatch" in events[0]["warnings"]
    assert asm.audit()["warnings"] == 1


def test_empty_record_flood_is_bounded():
    asm = tra.TlsRecordAssembler(max_empty_records=2)
    events = asm.feed(record(CT_APPDATA, b"") + record(CT_APPDATA, b""))
    assert [item["kind"] for item in events] == ["empty", "empty"]
    with pytest.raises(tra.RecordLayerError) as info:
        asm.feed(record(CT_APPDATA, b""))
    assert info.value.kind == "empty_record_flood"
    assert asm.errors()["empty_record_flood"] == 1


def test_change_cipher_spec_flood_is_bounded():
    asm = tra.TlsRecordAssembler(negotiated_version=0x0303, max_ccs_records=2)
    events = asm.feed(record(CT_CCS, b"\x01") + record(CT_CCS, b"\x01"))
    assert len(events) == 2
    with pytest.raises(tra.RecordLayerError) as info:
        asm.feed(record(CT_CCS, b"\x01"))
    assert info.value.kind == "ccs_flood"
    assert asm.errors()["ccs_flood"] == 1


def test_decrypt_failures_are_classified():
    asm = tra.TlsRecordAssembler()
    discard = asm.on_decrypt_failure("unknown_content_type")
    assert discard["action"] == "discard"
    assert discard["fatal"] is False
    assert asm.audit()["state"] == "handshake"
    fatal = asm.on_decrypt_failure("bad_record_mac")
    assert fatal["action"] == "fatal"
    assert fatal["fatal"] is True
    assert asm.errors()["decrypt_discard"] == 1
    assert asm.errors()["decrypt_fatal"] == 1
    assert asm.audit()["state"] == "closed"


def test_compact_preserves_partial_record():
    asm = tra.TlsRecordAssembler()
    data = record(CT_HANDSHAKE, handshake(1, b"abcdefghij"))
    asm.feed(data[:10])
    assert asm.pending() == 10
    assert asm.compact() == 10
    assert asm.pending() == 10
    events = asm.feed(data[10:])
    assert len(events) == 1
    assert events[0]["handshake_type"] == 1


def test_buffer_state_survives_three_reads():
    asm = tra.TlsRecordAssembler()
    data = record(CT_HANDSHAKE, handshake(1, b"ch"))
    assert asm.feed(data[:2]) == []
    assert asm.feed(data[2:5]) == []
    assert asm.pending() == 5
    events = asm.feed(data[5:])
    assert len(events) == 1
    assert asm.audit()["records_in"] == 1


def test_record_errors_are_counted_by_kind():
    asm = tra.TlsRecordAssembler(max_record_len=1024)
    kinds = {
        "record_overflow",
        "empty_record_flood",
        "ccs_flood",
        "unexpected_change_cipher_spec",
        "application_data_before_handshake",
        "application_data_after_close",
        "handshake_incomplete",
        "decrypt_fatal",
        "decrypt_discard",
    }
    assert kinds.issubset(set(asm.errors()))
    assert all(asm.errors()[name] == 0 for name in kinds)
    header = bytes([CT_HANDSHAKE, 0x03, 0x03]) + (2000).to_bytes(2, "big")
    with pytest.raises(tra.RecordLayerError):
        asm.feed(header)
    assert asm.errors()["record_overflow"] == 1
    assert asm.errors()["empty_record_flood"] == 0


def test_audit_metadata_after_session():
    asm = tra.TlsRecordAssembler(negotiated_version=0x0303)
    asm.feed(record(CT_HANDSHAKE, handshake(1, b"ch")))
    asm.feed(finished())
    asm.feed(record(CT_APPDATA, b"payload"))
    asm.feed(record(CT_ALERT, b"\x01\x00"))
    info = asm.audit()
    assert info["negotiated_version"] == 0x0303
    assert info["records_in"] == 4
    assert info["records_out"] == 4
    assert info["handshake_messages"] == 2
    assert info["state"] == "closed"
    assert info["warnings"] == 0
    assert isinstance(info["errors"], dict)
