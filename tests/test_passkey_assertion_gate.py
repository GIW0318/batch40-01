import hashlib
import json

import pytest

import passkey_assertion_gate as pg

RP = "example.com"
ORIGIN = "https://example.com"


class FakeVerifier:
    def __init__(self):
        self.calls = []

    def __call__(self, public_key, message, signature, algorithm):
        self.calls.append({
            "key": public_key,
            "message": message,
            "signature": signature,
            "alg": algorithm,
        })
        return signature == b"good"


def auth_data(flags=0x05, counter=0, rp=RP, extensions=b""):
    return (hashlib.sha256(rp.encode()).digest()
            + bytes([flags])
            + counter.to_bytes(4, "big")
            + extensions)


def client_json(kind, challenge, origin=ORIGIN):
    return json.dumps(
        {"type": kind, "challenge": challenge, "origin": origin},
        separators=(",", ":"),
    ).encode()


def setup(**overrides):
    verifier = FakeVerifier()
    args = {
        "verifier": verifier,
        "uv_required": True,
        "challenge_ttl": 300,
    }
    args.update(overrides)
    gate = pg.PasskeyAssertionGate(RP, [ORIGIN], **args)
    return gate, verifier


def registration_response(challenge, cred=b"cred-alice", handle=b"alice-handle",
                          alg="ES256", counter=0, flags=0x05, rp=RP,
                          origin=ORIGIN, signature=b"good", extensions=b""):
    return {
        "challenge": challenge,
        "client_data_json": client_json("webauthn.create", challenge, origin),
        "credential_id": cred,
        "public_key": b"pk-" + cred,
        "algorithm": alg,
        "user_handle": handle,
        "authenticator_data": auth_data(flags, counter, rp, extensions),
        "signature": signature,
    }


def assertion_response(challenge, cred=b"cred-alice", handle=b"alice-handle",
                       alg="ES256", counter=1, flags=0x05, rp=RP,
                       origin=ORIGIN, signature=b"good", extensions=b""):
    return {
        "challenge": challenge,
        "client_data_json": client_json("webauthn.get", challenge, origin),
        "credential_id": cred,
        "algorithm": alg,
        "user_handle": handle,
        "authenticator_data": auth_data(flags, counter, rp, extensions),
        "signature": signature,
    }


def register(gate, user="alice", cred=b"cred-alice", handle=b"alice-handle",
             alg="ES256", counter=0, now=0):
    begin = gate.register_begin("s1", user, action="register")
    response = registration_response(begin["challenge"], cred=cred,
                                     handle=handle, alg=alg, counter=counter)
    return gate.register_finish("s1", response, now)


def assert_once(gate, user="alice", session="s1", action="login",
                allow_credentials=None, now=10, **overrides):
    begin = gate.assert_begin(session, user, action=action,
                              allow_credentials=allow_credentials)
    response = assertion_response(begin["challenge"], **overrides)
    return gate.assert_finish(session, response, now)


def test_valid_registration_and_assertion_succeed():
    gate, verifier = setup()
    created = register(gate)
    assert created["credential_id"] == b"cred-alice"
    result = assert_once(gate)
    assert result["ok"] is True
    assert result["user_id"] == "alice"
    assert result["credential_id"] == b"cred-alice"
    assert len(verifier.calls) == 2


def test_challenge_is_single_use():
    gate, verifier = setup()
    register(gate)
    begin = gate.assert_begin("s1", "alice")
    response = assertion_response(begin["challenge"])
    first = gate.assert_finish("s1", response, 10)
    assert first["ok"] is True
    with pytest.raises(ValueError):
        gate.assert_finish("s1", response, 11)


def test_challenge_is_bound_to_session():
    gate, verifier = setup()
    register(gate)
    begin = gate.assert_begin("s1", "alice")
    response = assertion_response(begin["challenge"])
    with pytest.raises(ValueError):
        gate.assert_finish("s2", response, 10)


def test_register_challenge_cannot_be_used_for_assertion():
    gate, verifier = setup()
    register(gate)
    begin = gate.register_begin("s1", "alice", action="register")
    response = assertion_response(begin["challenge"])
    with pytest.raises(ValueError):
        gate.assert_finish("s1", response, 10)


def test_challenge_expires_on_the_server():
    gate, verifier = setup()
    begin = gate.register_begin("s1", "alice", action="register")
    response = registration_response(begin["challenge"])
    with pytest.raises(ValueError):
        gate.register_finish("s1", response, 301)


def test_rp_id_must_match_the_full_host():
    gate, verifier = setup()
    register(gate)
    with pytest.raises(ValueError):
        assert_once(gate, rp="evil-example.com")


def test_origin_scheme_is_part_of_the_origin():
    gate, verifier = setup()
    register(gate)
    with pytest.raises(ValueError):
        assert_once(gate, origin="http://example.com")


def test_origin_port_must_match_exactly():
    gate, verifier = setup()
    register(gate)
    with pytest.raises(ValueError):
        assert_once(gate, origin="https://example.com:8443")


def test_signature_counter_must_increase_strictly():
    gate, verifier = setup()
    register(gate, counter=0)
    with pytest.raises(ValueError):
        assert_once(gate, counter=0)
    advanced = assert_once(gate, counter=5)
    assert advanced["ok"] is True
    with pytest.raises(ValueError):
        assert_once(gate, counter=5)
    final = assert_once(gate, counter=6)
    assert final["ok"] is True


def test_user_presence_and_verification_bits_are_checked():
    gate, verifier = setup()
    register(gate)
    with pytest.raises(ValueError):
        assert_once(gate, flags=0x01)
    with pytest.raises(ValueError):
        assert_once(gate, flags=0x04)
    result = assert_once(gate, flags=0x05)
    assert result["ok"] is True


def test_credential_id_must_match_exactly():
    gate, verifier = setup()
    register(gate)
    with pytest.raises(ValueError):
        assert_once(gate, cred=b"cred-alic")
    with pytest.raises(ValueError):
        assert_once(gate, cred=b"cred-aliceX")


def test_user_handle_must_belong_to_the_credential_owner():
    gate, verifier = setup()
    register(gate)
    with pytest.raises(ValueError):
        assert_once(gate, handle=b"bob-handle")


def test_allow_credentials_must_be_owned_and_empty_means_none():
    gate, verifier = setup()
    register(gate)
    with pytest.raises(ValueError):
        gate.assert_begin("s2", "bob", allow_credentials=[b"cred-alice"])
    with pytest.raises(ValueError):
        gate.assert_begin("s3", "alice", allow_credentials=[])


def test_algorithm_is_whitelisted_and_taken_from_credential():
    gate, verifier = setup()
    begin = gate.register_begin("s1", "alice", action="register")
    bad_registration = registration_response(begin["challenge"], alg="HS256")
    with pytest.raises(ValueError):
        gate.register_finish("s1", bad_registration, 0)
    assert verifier.calls == []
    register(gate)
    calls_after_registration = len(verifier.calls)
    with pytest.raises(ValueError):
        assert_once(gate, alg="HS256")
    assert len(verifier.calls) == calls_after_registration


def test_signature_covers_authenticator_data_and_client_data_hash():
    gate, verifier = setup()
    register(gate)
    extensions = b"\x00\x01ext"
    begin = gate.assert_begin("s1", "alice")
    challenge = begin["challenge"]
    client_bytes = client_json("webauthn.get", challenge)
    response = assertion_response(challenge, counter=1, flags=0x85,
                                  extensions=extensions)
    result = gate.assert_finish("s1", response, 10)
    assert result["ok"] is True
    call = verifier.calls[-1]
    assert call["alg"] == "ES256"
    expected = (auth_data(0x85, 1, RP, extensions)
                + hashlib.sha256(client_bytes).digest())
    assert call["message"] == expected
