import base64
import hashlib
import hmac
import struct

import pytest

import totp_drift_ledger as tdl

SECRET = "JBSWY3DPEHPK3PXP"
SECRET2 = "JBSWY3DPEHPK3PXQ"
NEW_SECRET = "GEZDGNBVGY3TQOJQ"

TENANTS = {
    "acme": {"window": 1, "max_failures": 5},
    "slow": {"window": 2, "max_failures": 5},
    "strict": {"window": 0, "max_failures": 5},
}


def normalize(secret):
    text = secret.upper().replace(" ", "").replace("=", "")
    return text + "=" * ((8 - len(text) % 8) % 8)


def totp(secret, step, digits=6):
    key = base64.b32decode(normalize(secret))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def ledger(store=None, tenants=None):
    return tdl.TotpDriftLedger(tenants or TENANTS, store)


def enroll(ledger_obj, tenant="acme", user="alice", secret=SECRET, digits=6):
    return ledger_obj.enroll(tenant, user, secret, digits=digits)


def test_current_step_code_is_accepted():
    book = ledger()
    enroll(book)
    result = book.verify("acme", "alice", totp(SECRET, 100), 100 * 30 + 17)
    assert result["ok"] is True
    assert result["reason"] is None
    assert result["step"] == 100
    assert result["drift"] == 0


def test_previous_step_is_accepted_with_negative_drift():
    book = ledger()
    enroll(book)
    result = book.verify("acme", "alice", totp(SECRET, 99), 100 * 30 + 17)
    assert result["ok"] is True
    assert result["step"] == 99
    assert result["drift"] == -1


def test_code_beyond_window_is_rejected_with_reason():
    book = ledger()
    enroll(book)
    result = book.verify("acme", "alice", totp(SECRET, 102), 100 * 30 + 17)
    assert result["ok"] is False
    assert result["reason"] == "drift_out_of_window"


def test_tenant_windows_are_independent():
    book = ledger()
    enroll(book, tenant="slow", user="slow-user")
    enroll(book, tenant="strict", user="strict-user")
    slow = book.verify("slow", "slow-user", totp(SECRET, 102), 100 * 30 + 17)
    strict = book.verify("strict", "strict-user", totp(SECRET, 101), 100 * 30 + 17)
    assert slow["ok"] is True
    assert slow["drift"] == 2
    assert strict["ok"] is False
    assert strict["reason"] == "drift_out_of_window"


def test_time_step_uses_utc_floor():
    book = ledger()
    enroll(book, user="alice")
    enroll(book, user="bob")
    unaligned = 100 * 30 + 17
    aligned = 100 * 30
    late = book.verify("acme", "alice", totp(SECRET, 100), unaligned)
    boundary = book.verify("acme", "bob", totp(SECRET, 99), aligned - 1)
    assert late["step"] == 100
    assert boundary["step"] == 99


def test_consumed_code_cannot_be_replayed():
    book = ledger()
    enroll(book)
    code = totp(SECRET, 100)
    first = book.verify("acme", "alice", code, 100 * 30 + 5)
    second = book.verify("acme", "alice", code, 100 * 30 + 6)
    assert first["ok"] is True
    assert second["ok"] is False
    assert second["reason"] == "replay"


def test_base32_input_is_normalized():
    book = ledger()
    enroll(book, user="alice", secret="mzxw 6")
    enroll(book, user="bob", secret="MZXW6")
    canonical = totp("MZXW6===", 100)
    first = book.verify("acme", "alice", canonical, 100 * 30 + 5)
    second = book.verify("acme", "bob", canonical, 100 * 30 + 5)
    assert first["ok"] is True
    assert second["ok"] is True


def test_eight_digit_code_keeps_leading_zero():
    step = None
    for candidate in range(1000, 5000):
        value = totp(SECRET, candidate, 8)
        if value.startswith("0"):
            step = candidate
            break
    assert step is not None
    book = ledger()
    enroll(book, digits=8)
    code = totp(SECRET, step, 8)
    assert len(code) == 8
    result = book.verify("acme", "alice", code, step * 30 + 5)
    assert result["ok"] is True


def test_six_digit_authenticator_rejects_eight_digit_code():
    step = None
    for candidate in range(1000, 5000):
        value = totp(SECRET, candidate, 8)
        if value.startswith("0"):
            step = candidate
            break
    book = ledger()
    enroll(book, digits=6)
    result = book.verify("acme", "alice", totp(SECRET, step, 8), step * 30 + 5)
    assert result["ok"] is False


def test_key_rotation_overlap_accepts_both_keys():
    book = ledger()
    auth_id = enroll(book)
    book.rotate("acme", "alice", auth_id, NEW_SECRET, overlap_until=1000)
    old = book.verify("acme", "alice", totp(SECRET, 16), 16 * 30 + 5)
    new = book.verify("acme", "alice", totp(NEW_SECRET, 17), 17 * 30 + 5)
    assert old["ok"] is True
    assert new["ok"] is True


def test_old_key_is_rejected_after_overlap():
    book = ledger()
    auth_id = enroll(book)
    book.rotate("acme", "alice", auth_id, NEW_SECRET, overlap_until=1000)
    old = book.verify("acme", "alice", totp(SECRET, 33), 1010)
    new = book.verify("acme", "alice", totp(NEW_SECRET, 33), 1010)
    assert old["ok"] is False
    assert new["ok"] is True


def test_tenant_key_prefix_collision_is_isolated():
    book = ledger()
    enroll(book, tenant="acme", user="alice", secret=SECRET)
    enroll(book, tenant="slow", user="alice", secret=SECRET2)
    code = totp(SECRET2, 100)
    wrong = book.verify("acme", "alice", code, 100 * 30 + 5)
    right = book.verify("slow", "alice", code, 100 * 30 + 5)
    assert wrong["ok"] is False
    assert right["ok"] is True


def test_failure_counts_are_shared_across_instances():
    store = {}
    first = ledger(store)
    second = ledger(store)
    enroll(first)
    enroll(second)
    wrong = totp(SECRET, 200)
    one = first.verify("acme", "alice", wrong, 100 * 30 + 5)
    two = first.verify("acme", "alice", wrong, 100 * 30 + 6)
    three = second.verify("acme", "alice", wrong, 100 * 30 + 7)
    assert one["failures"] == 1
    assert two["failures"] == 2
    assert three["failures"] == 3


def test_each_authenticator_is_checked_and_unbind_removes_one():
    book = ledger()
    first_id = enroll(book, user="alice", secret=SECRET)
    second_id = enroll(book, user="alice", secret=SECRET2)
    one = book.verify("acme", "alice", totp(SECRET, 100), 100 * 30 + 5)
    two = book.verify("acme", "alice", totp(SECRET2, 100), 100 * 30 + 5)
    assert one["ok"] is True
    assert two["ok"] is True
    assert book.unbind("acme", "alice", first_id) is True
    removed = book.verify("acme", "alice", totp(SECRET, 101), 101 * 30 + 5)
    remaining = book.verify("acme", "alice", totp(SECRET2, 101), 101 * 30 + 5)
    assert removed["ok"] is False
    assert remaining["ok"] is True
    assert second_id != first_id


def test_audit_records_success_and_failure():
    book = ledger()
    enroll(book)
    good = book.verify("acme", "alice", totp(SECRET, 100), 100 * 30 + 5)
    bad = book.verify("acme", "alice", totp(SECRET, 200), 100 * 30 + 6)
    assert good["ok"] is True
    assert bad["ok"] is False
    entries = book.audit()
    assert len(entries) == 2
    assert [entry["seq"] for entry in entries] == [1, 2]
    assert [entry["ok"] for entry in entries] == [True, False]
    assert entries[1]["reason"] is not None
