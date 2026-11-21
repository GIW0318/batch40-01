import base64
import hashlib
import json

import pytest

import oidc_idtoken_verify as oidc

ISSUER = "https://issuer.example.com"
CLIENT = "client-123"

K1 = {"kid": "k1", "kty": "RSA", "alg": "RS256", "n": "n1", "e": "AQAB"}
K2 = {"kid": "k2", "kty": "RSA", "alg": "RS256", "n": "n2", "e": "AQAB"}


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt(header, payload, signature=b"good"):
    head = b64url(json.dumps(header, separators=(",", ":")).encode())
    body = b64url(json.dumps(payload, separators=(",", ":")).encode())
    return ("%s.%s.%s" % (head, body, b64url(signature))).encode()


class Verifier:
    def __init__(self):
        self.calls = []

    def __call__(self, algorithm, key, signing_input, signature):
        self.calls.append({
            "alg": algorithm,
            "key": key,
            "input": signing_input,
        })
        return signature == b"good"


def header(**overrides):
    data = {"alg": "RS256", "kid": "k1"}
    data.update(overrides)
    return data


def claims(nonce, exp=1300, iat=990, **overrides):
    data = {
        "iss": ISSUER,
        "aud": CLIENT,
        "sub": "alice",
        "exp": exp,
        "iat": iat,
        "nonce": nonce,
        "email_verified": True,
    }
    data.update(overrides)
    return data


def make_gate(jwks=None, provider_fn=None, verifier=None, **kwargs):
    if provider_fn is None:
        provider_fn = lambda: {"keys": list(jwks or [K1])}
    fake = verifier or Verifier()
    gate = oidc.OidcIdTokenVerify(ISSUER, CLIENT, provider_fn,
                                  verifier=fake, **kwargs)
    return gate, fake


def test_valid_token_is_accepted():
    gate, verifier = make_gate([K1])
    nonce = gate.begin("s1")["nonce"]
    token = jwt(header(), claims(nonce))
    result = gate.verify(token, "s1", now=1000)
    assert result["sub"] == "alice"
    assert result["claims"]["iss"] == ISSUER
    assert verifier.calls[-1]["key"]["kid"] == "k1"


def test_none_and_hmac_algorithms_are_rejected():
    gate, verifier = make_gate([K1])
    nonce = gate.begin("s1")["nonce"]
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(jwt(header(alg="none"), claims(nonce)), "s1", now=1000)
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(jwt(header(alg="HS256"), claims(nonce)), "s1", now=1000)
    assert verifier.calls == []


def test_missing_kid_does_not_try_every_key():
    gate, verifier = make_gate([K1, K2])
    nonce = gate.begin("s1")["nonce"]
    token = jwt({"alg": "RS256"}, claims(nonce))
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(token, "s1", now=1000)
    assert verifier.calls == []


def test_unknown_kid_refreshes_jwks_once():
    state = {"calls": 0}
    sets = [[K1], [K1, K2]]

    def provider():
        index = min(state["calls"], 1)
        state["calls"] += 1
        return {"keys": list(sets[index])}

    gate, verifier = make_gate(provider_fn=provider)
    nonce = gate.begin("s1")["nonce"]
    token = jwt(header(kid="k2"), claims(nonce))
    result = gate.verify(token, "s1", now=1000)
    assert result["sub"] == "alice"
    assert verifier.calls[-1]["key"]["kid"] == "k2"
    info = gate.snapshot()
    assert info["jwks_refreshes"] >= 1
    assert info["jwks_refreshes"] <= 2


def test_issuer_must_match_exactly():
    gate, verifier = make_gate([K1])
    nonce = gate.begin("s1")["nonce"]
    token = jwt(header(), claims(nonce, iss=ISSUER + "/extra"))
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(token, "s1", now=1000)


def test_multiple_audiences_require_azp():
    gate, verifier = make_gate([K1])
    nonce = gate.begin("s1")["nonce"]
    token = jwt(header(), claims(nonce, aud=[CLIENT, "other-client"]))
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(token, "s1", now=1000)


def test_multiple_audiences_with_matching_azp_are_accepted():
    gate, verifier = make_gate([K1])
    nonce = gate.begin("s1")["nonce"]
    token = jwt(header(), claims(nonce, aud=[CLIENT, "other-client"],
                                 azp=CLIENT))
    result = gate.verify(token, "s1", now=1000)
    assert result["sub"] == "alice"


def test_azp_must_point_to_this_client():
    gate, verifier = make_gate([K1])
    nonce = gate.begin("s1")["nonce"]
    token = jwt(header(), claims(nonce, aud=[CLIENT, "other-client"],
                                 azp="other-client"))
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(token, "s1", now=1000)


def test_nonce_must_match_the_session():
    gate, verifier = make_gate([K1])
    gate.begin("s1")
    token = jwt(header(), claims("not-the-nonce"))
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(token, "s1", now=1000)


def test_nonce_cannot_be_replayed():
    gate, verifier = make_gate([K1])
    nonce = gate.begin("s1")["nonce"]
    first = jwt(header(), claims(nonce))
    assert gate.verify(first, "s1", now=1000)["sub"] == "alice"
    second = jwt(header(), claims(nonce))
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(second, "s1", now=1001)


def test_issued_at_in_the_future_is_rejected():
    gate, verifier = make_gate([K1])
    nonce = gate.begin("s1")["nonce"]
    future = jwt(header(), claims(nonce, iat=1061))
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(future, "s1", now=1000)
    nonce2 = gate.begin("s2")["nonce"]
    within_skew = jwt(header(), claims(nonce2, iat=1060))
    assert gate.verify(within_skew, "s2", now=1000)["sub"] == "alice"


def test_expiry_is_checked_on_the_server_at_the_boundary():
    gate, verifier = make_gate([K1])
    nonce = gate.begin("s1")["nonce"]
    expired = jwt(header(), claims(nonce, exp=1000))
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(expired, "s1", now=1000)
    nonce2 = gate.begin("s2")["nonce"]
    alive = jwt(header(), claims(nonce2, exp=1001))
    assert gate.verify(alive, "s2", now=1000)["sub"] == "alice"


def test_at_hash_must_match_the_access_token():
    gate, verifier = make_gate([K1])
    access = b"access-token"
    nonce = gate.begin("s1")["nonce"]
    missing = jwt(header(), claims(nonce))
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(missing, "s1", access_token=access, now=1000)
    nonce2 = gate.begin("s2")["nonce"]
    wrong = jwt(header(), claims(nonce2, at_hash="AAAA"))
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(wrong, "s2", access_token=access, now=1000)
    nonce3 = gate.begin("s3")["nonce"]
    correct = b64url(hashlib.sha256(access).digest()[:16])
    right = jwt(header(), claims(nonce3, at_hash=correct))
    assert gate.verify(right, "s3", access_token=access, now=1000)["sub"] == "alice"


def test_claim_types_are_enforced():
    gate, verifier = make_gate([K1])
    nonce = gate.begin("s1")["nonce"]
    string_false = jwt(header(), claims(nonce, email_verified="false"))
    with pytest.raises(oidc.OidcTokenError):
        gate.verify(string_false, "s1", now=1000)
    nonce2 = gate.begin("s2")["nonce"]
    boolean_false = jwt(header(), claims(nonce2, email_verified=False))
    assert gate.verify(boolean_false, "s2", now=1000)["sub"] == "alice"


def test_errors_are_uniform_for_all_failures():
    gate, verifier = make_gate([K1])
    nonce = gate.begin("s1")["nonce"]
    bad_issuer = jwt(header(), claims(nonce, iss="https://evil.example.com"))
    with pytest.raises(oidc.OidcTokenError) as first:
        gate.verify(bad_issuer, "s1", now=1000)
    nonce2 = gate.begin("s2")["nonce"]
    bad_audience = jwt(header(), claims(nonce2, aud="someone-else"))
    with pytest.raises(oidc.OidcTokenError) as second:
        gate.verify(bad_audience, "s2", now=1000)
    assert first.value.code == "invalid_token"
    assert second.value.code == "invalid_token"
    assert str(first.value) == str(second.value) == "invalid_token"
