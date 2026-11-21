import pytest

import audit_hash_chain as ahc


def chain(records=()):
    result = ahc.AuditHashChain(
        version=2, supported_versions=(1, 2), salt="pepper"
    )
    for record in records:
        result.append(record)
    return result


def codes(result):
    return [issue["code"] for issue in result["issues"]]


def test_append_links_each_entry_to_the_previous_one():
    ledger = ahc.AuditHashChain(supported_versions=(1, 2))
    first = ledger.append({"event": "a"})
    second = ledger.append({"event": "b"})
    assert first["seq"] == 0
    assert second["seq"] == 1
    assert first["prev"] == "0" * 64
    assert second["prev"] == first["hash"]
    assert len(first["hash"]) == 64


def test_valid_chain_verifies_cleanly():
    ledger = chain([{"e": 1}, {"e": 2}, {"e": 3}])
    assert ledger.verify() == {"ok": True, "issues": []}


def test_tampered_record_is_detected():
    ledger = chain([{"amount": 10}, {"amount": 20}])
    ledger.entries[1]["record"]["amount"] = 99
    result = ledger.verify()
    assert result["ok"] is False
    assert "hash_mismatch" in codes(result)


def test_deleted_middle_entry_is_a_sequence_gap():
    ledger = chain([{"e": 1}, {"e": 2}, {"e": 3}])
    del ledger.entries[1]
    result = ledger.verify()
    assert result["ok"] is False
    assert "sequence_gap" in codes(result)


def test_entries_are_verified_by_sequence_not_wall_clock():
    ledger = chain([{"t": 100}, {"t": 50}, {"t": 10}])
    result = ledger.verify(list(reversed(ledger.entries)))
    assert result["ok"] is True


def test_anchor_describes_the_current_tail():
    ledger = chain([{"e": 1}, {"e": 2}])
    anchor = ledger.anchor("2026-11-20")
    assert anchor == {
        "day": "2026-11-20",
        "seq": 1,
        "hash": ledger.entries[-1]["hash"],
        "length": 2,
    }


def test_older_anchor_still_verifies_against_a_longer_chain():
    ledger = chain([{"e": 1}, {"e": 2}])
    anchor = ledger.anchor("day-1")
    ledger.append({"e": 3})
    assert ledger.verify_anchor(anchor) is True


def test_tail_deletion_is_detected_by_the_anchor_length():
    ledger = chain([{"e": 1}, {"e": 2}, {"e": 3}])
    anchor = ledger.anchor("day-1")
    del ledger.entries[-1]
    del ledger.entries[-1]
    assert ledger.verify_anchor(anchor) is False


def test_snapshot_exposes_only_length_and_head():
    ledger = chain([{"e": 1}])
    snapshot = ledger.snapshot()
    assert set(snapshot) == {"length", "head"}
    assert snapshot["length"] == 1
    assert snapshot["head"] == ledger.entries[0]["hash"]
    assert "salt" not in snapshot


def test_inclusion_proof_verifies_against_the_batch_root():
    ledger = chain([{"e": index} for index in range(5)])
    proof = ledger.proof(2)
    assert set(proof) == {"index", "hash", "path", "root"}
    assert proof["index"] == 2
    assert ledger.verify_proof(ledger.entries[2]["hash"], proof) is True


def test_proof_does_not_verify_against_another_batch_root():
    ledger = chain([{"e": index} for index in range(5)])
    proof = ledger.proof(1)
    assert ledger.verify_proof(
        ledger.entries[1]["hash"], proof, root="0" * 64
    ) is False


def test_stale_expected_length_is_a_conflict():
    ledger = ahc.AuditHashChain()
    ledger.append({"e": 1})
    with pytest.raises(ahc.ConflictError):
        ledger.append({"e": 2}, expected_length=0)
    entry = ledger.append({"e": 3}, expected_length=1)
    assert entry["seq"] == 1


def test_older_serialization_versions_are_verified_not_skipped():
    ledger = ahc.AuditHashChain(version=2, supported_versions=(1, 2))
    prev = "0" * 64
    entries = []
    for index, record in enumerate([{"e": 1}, {"e": 2}]):
        digest = ledger.entry_hash(index, record, prev, version=1)
        entries.append({
            "seq": index,
            "record": record,
            "prev": prev,
            "hash": digest,
            "version": 1,
        })
        prev = digest
    assert ledger.verify(entries)["ok"] is True


def test_unsupported_serialization_version_is_reported():
    ledger = ahc.AuditHashChain(version=2, supported_versions=(1, 2))
    prev = "0" * 64
    entries = []
    for index, record in enumerate([{"e": 1}]):
        digest = ledger.entry_hash(index, record, prev, version=1)
        entries.append({
            "seq": index,
            "record": record,
            "prev": prev,
            "hash": digest,
            "version": 9,
        })
        prev = digest
    result = ledger.verify(entries)
    assert result["ok"] is False
    assert "unsupported_version" in codes(result)


def test_anchor_detects_a_tampered_anchored_record():
    ledger = chain([{"e": 1}, {"e": 2}])
    anchor = ledger.anchor("day-1")
    ledger.entries[1]["record"]["e"] = 99
    assert ledger.verify_anchor(anchor) is False
