import pytest

import canonical_sig_verify as csv


KEYS = {"k1": b"secret-one", "k2": b"secret-one"}


def test_integral_floats_share_one_representation():
    canonical = csv.CanonicalSigVerify(KEYS)
    assert (
        canonical.canonicalize({"n": 1})
        == canonical.canonicalize({"n": 1.0})
        == canonical.canonicalize({"n": 1e0})
    )
    assert canonical.canonicalize({"n": 1.5}) != canonical.canonicalize({"n": 1})


def test_nan_and_infinity_are_rejected():
    canonical = csv.CanonicalSigVerify(KEYS)
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            canonical.canonicalize({"n": value})


def test_non_ascii_strings_are_utf8_not_escaped():
    data = csv.CanonicalSigVerify(KEYS).canonicalize({"s": "é"})
    assert "é".encode("utf-8") in data
    assert b"\\u00e9" not in data


def test_control_characters_use_standard_escapes():
    canonical = csv.CanonicalSigVerify(KEYS)
    obj = {"s": "\x01\n"}
    data = canonical.canonicalize(obj)
    assert b"\\u0001" in data
    assert b"\\n" in data
    assert canonical.verify(obj, canonical.sign(obj, "k1"), "k1") is True


def test_keys_sort_by_code_point_not_locale():
    data = csv.CanonicalSigVerify(KEYS).canonicalize(
        {"é": 1, "b": 2, "a": 3, "Z": 4}
    )
    assert data.index(b'"Z"') < data.index(b'"a"')
    assert data.index(b'"a"') < data.index(b'"b"')
    assert data.index(b'"b"') < data.index('"é"'.encode("utf-8"))


def test_duplicate_keys_are_rejected_by_loads():
    with pytest.raises(ValueError):
        csv.CanonicalSigVerify(KEYS).loads('{"a": 1, "a": 2}')


def test_canonical_version_is_part_of_the_signature():
    canonical = csv.CanonicalSigVerify(KEYS)
    envelope = canonical.envelope({"a": 1}, "k1")
    assert envelope["canonical_version"] == "1"
    assert canonical.verify_envelope(envelope, {"a": 1}) is True
    tampered = dict(envelope)
    tampered["canonical_version"] = "2"
    assert canonical.verify_envelope(tampered, {"a": 1}) is False
    other = csv.CanonicalSigVerify(KEYS, version="2")
    assert other.verify_envelope(envelope, {"a": 1}) is False


def test_verification_binds_the_recorded_digest():
    canonical = csv.CanonicalSigVerify(KEYS)
    envelope = canonical.envelope({"a": 1}, "k1")
    assert canonical.verify_envelope(envelope, {"a": 2}) is False


def test_key_id_is_inside_the_signed_scope():
    canonical = csv.CanonicalSigVerify(KEYS)
    signature = canonical.sign({"a": 1}, "k1")
    assert canonical.verify({"a": 1}, signature, "k1") is True
    assert canonical.verify({"a": 1}, signature, "k2") is False
    envelope = canonical.envelope({"a": 1}, "k1")
    swapped = dict(envelope)
    swapped["key_id"] = "k2"
    assert canonical.verify_envelope(swapped, {"a": 1}) is False


def test_unknown_key_id_is_rejected():
    canonical = csv.CanonicalSigVerify(KEYS)
    with pytest.raises(ValueError):
        canonical.sign({"a": 1}, "ghost")
    envelope = canonical.envelope({"a": 1}, "k1")
    envelope["key_id"] = "ghost"
    assert canonical.verify_envelope(envelope, {"a": 1}) is False


def test_signature_encoding_is_strict():
    canonical = csv.CanonicalSigVerify(KEYS)
    signature = canonical.sign({"a": 1}, "k1")
    assert canonical.verify({"a": 1}, signature, "k1") is True
    assert canonical.verify({"a": 1}, signature + " ", "k1") is False
    assert canonical.verify({"a": 1}, signature.rstrip("="), "k1") is False
    broken = signature[:4] + "\n" + signature[4:]
    assert canonical.verify({"a": 1}, broken, "k1") is False
    with pytest.raises(ValueError):
        canonical.decode_signature("not base64!")


def test_null_and_missing_are_distinct():
    canonical = csv.CanonicalSigVerify(KEYS)
    assert canonical.canonicalize({"a": None}) != canonical.canonicalize({})
    assert (
        canonical.envelope({"a": None}, "k1")["digest"]
        != canonical.envelope({}, "k1")["digest"]
    )


def test_sign_and_verify_round_trip():
    canonical = csv.CanonicalSigVerify(KEYS)
    obj = {"b": [1, 2, {"c": None}], "a": "x"}
    signature = canonical.sign(obj, "k1")
    assert canonical.verify(obj, signature, "k1") is True
    changed = {"b": [1, 2, {"c": None}], "a": "y"}
    assert canonical.verify(changed, signature, "k1") is False


def test_tampered_envelope_digest_fails():
    canonical = csv.CanonicalSigVerify(KEYS)
    envelope = canonical.envelope({"a": 1}, "k1")
    tampered = dict(envelope)
    tampered["digest"] = "0" * 64
    assert canonical.verify_envelope(tampered, {"a": 1}) is False


def test_loads_rejects_trailing_data():
    canonical = csv.CanonicalSigVerify(KEYS)
    with pytest.raises(ValueError):
        canonical.loads('{"a": 1} trailing')
    assert canonical.loads('{"a": 1}') == {"a": 1}
