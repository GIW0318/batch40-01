"""OIDC id_token signature and claim verification."""

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from collections import OrderedDict


class OidcTokenError(Exception):
    """Uniform external error for every invalid id_token."""

    code = "invalid_token"

    def __init__(self, reason=""):
        super().__init__("invalid_token")
        self.reason = reason
        self.code = "invalid_token"


_ASYMMETRIC_ALGORITHMS = frozenset({
    "RS256", "RS384", "RS512",
    "PS256", "PS384", "PS512",
    "ES256", "ES384", "ES512",
    "EdDSA",
})

_ALGORITHM_FAMILIES = {
    "RS256": "RSA", "RS384": "RSA", "RS512": "RSA",
    "PS256": "RSA", "PS384": "RSA", "PS512": "RSA",
    "ES256": "EC", "ES384": "EC", "ES512": "EC",
    "EdDSA": "Ed25519",
}

_HASH_ALGORITHMS = {
    "RS256": hashlib.sha256, "PS256": hashlib.sha256, "ES256": hashlib.sha256,
    "RS384": hashlib.sha384, "PS384": hashlib.sha384, "ES384": hashlib.sha384,
    "RS512": hashlib.sha512, "PS512": hashlib.sha512, "ES512": hashlib.sha512,
    "EdDSA": hashlib.sha256,
}

_B64URL_RE = re.compile(rb"\A[A-Za-z0-9_-]+\Z")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _b64url_decode(value, field):
    if not isinstance(value, bytes) or not _B64URL_RE.match(value):
        raise OidcTokenError("invalid %s encoding" % field)
    if len(value) % 4 == 1:
        raise OidcTokenError("invalid %s encoding" % field)
    padding = b"=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (binascii.Error, ValueError):
        raise OidcTokenError("invalid %s encoding" % field)


def _b64url_encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json_object(raw, field):
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, ValueError, TypeError):
        raise OidcTokenError("invalid %s" % field)
    if not isinstance(value, dict):
        raise OidcTokenError("invalid %s" % field)
    return value


def _normalise_token_value(value, field):
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise OidcTokenError("invalid %s" % field)


def _digest_for_algorithm(algorithm):
    try:
        return _HASH_ALGORITHMS[algorithm]
    except KeyError:
        raise OidcTokenError("unsupported algorithm")


def _check_token_hash(claims, name, value, algorithm):
    if value is None:
        if name in claims:
            raise OidcTokenError("unverifiable %s claim" % name)
        return
    expected = claims.get(name)
    if not isinstance(expected, str):
        raise OidcTokenError("invalid %s claim" % name)
    raw = _normalise_token_value(value, name.replace("_hash", " token"))
    digest = _digest_for_algorithm(algorithm)(raw).digest()
    calculated = _b64url_encode(digest[:len(digest) // 2])
    if not hmac.compare_digest(expected, calculated):
        raise OidcTokenError("%s mismatch" % name)


def _as_bytes(value):
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("ascii")
    raise OidcTokenError("invalid JWK member")


def _jwk_public_key(jwk):
    try:
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
    except ImportError:
        raise OidcTokenError("cryptography backend unavailable")

    kty = jwk.get("kty")
    if kty == "RSA":
        n = _b64url_decode(_as_bytes(jwk.get("n")), "jwk n")
        e = _b64url_decode(_as_bytes(jwk.get("e")), "jwk e")
        return rsa.RSAPublicNumbers(int.from_bytes(e, "big"),
                                    int.from_bytes(n, "big")).public_key()
    if kty == "EC":
        curve_name = jwk.get("crv")
        curves = {
            "P-256": ec.SECP256R1,
            "P-384": ec.SECP384R1,
            "P-521": ec.SECP521R1,
        }
        if curve_name not in curves:
            raise OidcTokenError("unsupported EC curve")
        x = int.from_bytes(_b64url_decode(_as_bytes(jwk.get("x")),
                                          "jwk x"), "big")
        y = int.from_bytes(_b64url_decode(_as_bytes(jwk.get("y")),
                                          "jwk y"), "big")
        curve = curves[curve_name]()
        return ec.EllipticCurvePublicNumbers(x, y, curve).public_key()
    if kty == "OKP":
        if jwk.get("crv") != "Ed25519":
            raise OidcTokenError("unsupported OKP curve")
        raw = _b64url_decode(_as_bytes(jwk.get("x")), "jwk x")
        return ed25519.Ed25519PublicKey.from_public_bytes(raw)
    raise OidcTokenError("unsupported key type")


def _default_verifier(algorithm, key, signing_input, signature):
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import (
            ec, ed25519, padding, rsa,
        )
    except ImportError:
        raise OidcTokenError("cryptography backend unavailable")

    family = _ALGORITHM_FAMILIES.get(algorithm)
    if family is None:
        raise OidcTokenError("unsupported algorithm")

    public_key = _jwk_public_key(key)
    hash_algorithm = {
        "RS256": hashes.SHA256, "RS384": hashes.SHA384,
        "RS512": hashes.SHA512, "PS256": hashes.SHA256,
        "PS384": hashes.SHA384, "PS512": hashes.SHA512,
        "ES256": hashes.SHA256, "ES384": hashes.SHA384,
        "ES512": hashes.SHA512,
    }
    try:
        if family == "RSA":
            if not isinstance(public_key, rsa.RSAPublicKey):
                raise OidcTokenError("key type mismatch")
            digest = hash_algorithm[algorithm]()
            if algorithm.startswith("PS"):
                pad = padding.PSS(
                    mgf=padding.MGF1(digest),
                    salt_length=digest.digest_size,
                )
            else:
                pad = padding.PKCS1v15()
            public_key.verify(signature, signing_input, pad, digest)
        elif family == "EC":
            if not isinstance(public_key, ec.EllipticCurvePublicKey):
                raise OidcTokenError("key type mismatch")
            expected_curves = {
                "ES256": ec.SECP256R1,
                "ES384": ec.SECP384R1,
                "ES512": ec.SECP521R1,
            }
            if not isinstance(public_key.curve, expected_curves[algorithm]):
                raise OidcTokenError("key type mismatch")
            half = (len(signature) + 1) // 2
            if len(signature) % 2 or half == 0:
                raise OidcTokenError("invalid ECDSA signature")
            r = int.from_bytes(signature[:half], "big")
            s = int.from_bytes(signature[half:], "big")
            from cryptography.hazmat.primitives.asymmetric.utils import (
                encode_dss_signature,
            )
            der_signature = encode_dss_signature(r, s)
            public_key.verify(
                der_signature, signing_input,
                ec.ECDSA(hash_algorithm[algorithm]()),
            )
        else:
            if not isinstance(public_key, ed25519.Ed25519PublicKey):
                raise OidcTokenError("key type mismatch")
            public_key.verify(signature, signing_input)
    except InvalidSignature:
        raise OidcTokenError("signature mismatch")
    except OidcTokenError:
        raise
    except Exception:
        raise OidcTokenError("invalid signature")
    return True


class OidcIdTokenVerify:
    """Verifies compact JWT id_tokens against JWKS and OIDC claim rules."""

    def __init__(self, issuer, client_id, jwks_provider, verifier=None,
                 allowed_algorithms=("RS256", "ES256"), clock_skew=60,
                 max_jwks_refresh=2, jwks_cache_ttl=300,
                 max_jwks_keys=64, max_sessions=4096):
        if not isinstance(issuer, str) or not issuer:
            raise ValueError("issuer must be a non-empty string")
        if not isinstance(client_id, str) or not client_id:
            raise ValueError("client_id must be a non-empty string")
        if not callable(jwks_provider):
            raise ValueError("jwks_provider must be callable")
        if verifier is not None and not callable(verifier):
            raise ValueError("verifier must be callable")
        if not isinstance(allowed_algorithms, (list, tuple, set, frozenset)):
            raise ValueError("allowed_algorithms must be a collection")
        algorithms = frozenset(allowed_algorithms)
        if not algorithms:
            raise ValueError("at least one algorithm is required")
        if not algorithms.issubset(_ASYMMETRIC_ALGORITHMS):
            raise ValueError("only asymmetric signature algorithms are allowed")
        if not _is_number(clock_skew) or clock_skew < 0:
            raise ValueError("clock_skew must be a non-negative number")
        if not _is_int(max_jwks_refresh) or max_jwks_refresh < 0:
            raise ValueError("max_jwks_refresh must be a non-negative integer")
        if not _is_number(jwks_cache_ttl) or jwks_cache_ttl < 0:
            raise ValueError("jwks_cache_ttl must be a non-negative number")
        if not _is_int(max_jwks_keys) or max_jwks_keys < 1:
            raise ValueError("max_jwks_keys must be a positive integer")
        if not _is_int(max_sessions) or max_sessions < 1:
            raise ValueError("max_sessions must be a positive integer")

        self.issuer = issuer
        self.client_id = client_id
        self._jwks_provider = jwks_provider
        self._verifier = verifier or _default_verifier
        self.allowed_algorithms = algorithms
        self.clock_skew = clock_skew
        self.max_jwks_refresh = max_jwks_refresh
        self.jwks_cache_ttl = jwks_cache_ttl
        self.max_jwks_keys = max_jwks_keys
        self.max_sessions = max_sessions

        self._jwks = None
        self._jwks_fetched_at = None
        self._jwks_refreshes = 0
        self._sessions = OrderedDict()
        self._used_nonces = OrderedDict()
        self._lock = threading.RLock()

    def begin(self, session_id, nonce=None):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        if nonce is None:
            nonce = secrets.token_urlsafe(32)
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("nonce must be a non-empty string")
        with self._lock:
            self._sessions[session_id] = {
                "nonce": nonce,
                "used": False,
            }
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
        return {"session_id": session_id, "nonce": nonce}

    def verify(self, token, session_id, access_token=None, code=None, now=0):
        try:
            return self._verify(token, session_id, access_token, code, now)
        except OidcTokenError:
            raise
        except Exception:
            raise OidcTokenError("verification failed")

    def _verify(self, token, session_id, access_token, code, now):
        if not isinstance(session_id, str) or not session_id:
            raise OidcTokenError("invalid session")
        if now is None:
            now = time.time()
        if not _is_number(now):
            raise OidcTokenError("invalid verification time")

        token = _normalise_token_value(token, "token")
        parts = token.split(b".")
        if len(parts) != 3 or not parts[0] or not parts[1] or not parts[2]:
            raise OidcTokenError("invalid token structure")

        header = _json_object(_b64url_decode(parts[0], "header"), "header")
        claims = _json_object(_b64url_decode(parts[1], "payload"), "payload")

        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or not algorithm:
            raise OidcTokenError("missing algorithm")
        if algorithm == "none" or algorithm.startswith("HS"):
            raise OidcTokenError("algorithm not allowed")
        if algorithm not in self.allowed_algorithms:
            raise OidcTokenError("algorithm not allowed")

        crit = header.get("crit")
        if crit is not None:
            if not isinstance(crit, list) or any(
                    not isinstance(item, str) or not item for item in crit):
                raise OidcTokenError("invalid critical header")
            if crit:
                raise OidcTokenError("unsupported critical header")

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise OidcTokenError("missing key id")

        signing_input = parts[0] + b"." + parts[1]
        signature = _b64url_decode(parts[2], "signature")
        key = self._resolve_key(kid, algorithm, now)
        try:
            valid = self._verifier(algorithm, key, signing_input, signature)
        except OidcTokenError:
            raise
        except Exception:
            raise OidcTokenError("signature verification failed")
        if not valid:
            raise OidcTokenError("signature verification failed")

        self._validate_claims(claims, session_id, access_token, code,
                              algorithm, now)
        self._consume_nonce(session_id, claims["nonce"])
        return {"sub": claims["sub"], "claims": claims}

    def _validate_claims(self, claims, session_id, access_token, code,
                         algorithm, now):
        for name in ("iss", "sub", "aud", "exp", "iat", "nonce"):
            if name not in claims:
                raise OidcTokenError("missing %s claim" % name)

        issuer = claims["iss"]
        if not isinstance(issuer, str) or issuer != self.issuer:
            raise OidcTokenError("issuer mismatch")

        sub = claims["sub"]
        if not isinstance(sub, str) or not sub:
            raise OidcTokenError("invalid sub claim")

        audience = claims["aud"]
        if isinstance(audience, str):
            if not audience:
                raise OidcTokenError("invalid aud claim")
            audiences = [audience]
        elif isinstance(audience, list):
            if not audience or any(
                    not isinstance(item, str) or not item
                    for item in audience):
                raise OidcTokenError("invalid aud claim")
            audiences = audience
        else:
            raise OidcTokenError("invalid aud claim")
        if self.client_id not in audiences:
            raise OidcTokenError("audience mismatch")

        azp = claims.get("azp")
        if azp is not None and (not isinstance(azp, str) or not azp):
            raise OidcTokenError("invalid azp claim")
        if len(set(audiences)) > 1:
            if not isinstance(azp, str) or azp != self.client_id:
                raise OidcTokenError("invalid azp claim")
        elif azp is not None and azp != self.client_id:
            raise OidcTokenError("invalid azp claim")

        exp = claims["exp"]
        if not _is_number(exp):
            raise OidcTokenError("invalid exp claim")
        if exp <= now:
            raise OidcTokenError("token expired")

        iat = claims["iat"]
        if not _is_number(iat):
            raise OidcTokenError("invalid iat claim")
        if iat > now + self.clock_skew:
            raise OidcTokenError("token issued in the future")

        nbf = claims.get("nbf")
        if nbf is not None:
            if not _is_number(nbf):
                raise OidcTokenError("invalid nbf claim")
            if nbf > now + self.clock_skew:
                raise OidcTokenError("token not yet valid")

        nonce = claims["nonce"]
        if not isinstance(nonce, str) or not nonce:
            raise OidcTokenError("invalid nonce claim")

        if "email_verified" in claims and not isinstance(
                claims["email_verified"], bool):
            raise OidcTokenError("invalid email_verified claim")

        _check_token_hash(claims, "at_hash", access_token, algorithm)
        _check_token_hash(claims, "c_hash", code, algorithm)

    def _consume_nonce(self, session_id, nonce):
        digest = hashlib.sha256(nonce.encode("utf-8")).digest()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise OidcTokenError("unknown session")
            if session["used"]:
                raise OidcTokenError("nonce already used")
            if digest in self._used_nonces:
                raise OidcTokenError("nonce already used")
            if not hmac.compare_digest(session["nonce"], nonce):
                raise OidcTokenError("nonce mismatch")
            session["used"] = True
            self._used_nonces[digest] = True
            self._used_nonces.move_to_end(digest)
            while len(self._used_nonces) > self.max_sessions * 4:
                self._used_nonces.popitem(last=False)

    def _ensure_jwks(self, now, force=False):
        with self._lock:
            stale = (
                self._jwks is None
                or self._jwks_fetched_at is None
                or now < self._jwks_fetched_at
                or now - self._jwks_fetched_at >= self.jwks_cache_ttl
            )
            if not force and not stale:
                return
            if force and not stale and self.max_jwks_refresh == 0:
                return
            self._load_jwks(now, count_refresh=force)

    def _load_jwks(self, now, count_refresh):
        try:
            raw = self._jwks_provider()
        except Exception:
            raise OidcTokenError("JWKS provider failed")
        if isinstance(raw, dict):
            keys = raw.get("keys")
        else:
            keys = raw
        if not isinstance(keys, list):
            raise OidcTokenError("invalid JWKS")

        validated = []
        seen_kids = set()
        for key in keys:
            if not isinstance(key, dict):
                raise OidcTokenError("invalid JWKS key")
            kid = key.get("kid")
            kty = key.get("kty")
            if not isinstance(kid, str) or not kid:
                raise OidcTokenError("invalid JWKS key id")
            if kid in seen_kids:
                raise OidcTokenError("duplicate JWKS key id")
            if not isinstance(kty, str) or not kty:
                raise OidcTokenError("invalid JWKS key type")
            use = key.get("use")
            if use is not None and use != "sig":
                continue
            key_ops = key.get("key_ops")
            if key_ops is not None:
                if not isinstance(key_ops, list) or "verify" not in key_ops:
                    continue
            seen_kids.add(kid)
            validated.append(dict(key))
            if len(validated) >= self.max_jwks_keys:
                break

        with self._lock:
            self._jwks = validated
            self._jwks_fetched_at = now
            if count_refresh:
                self._jwks_refreshes += 1

    def _find_key(self, kid, algorithm):
        family = _ALGORITHM_FAMILIES[algorithm]
        matches = [key for key in self._jwks if key.get("kid") == kid]
        for key in matches:
            if key.get("kty") != family:
                continue
            key_alg = key.get("alg")
            if key_alg is not None and key_alg != algorithm:
                continue
            return key
        return None

    def _resolve_key(self, kid, algorithm, now):
        self._ensure_jwks(now)
        key = self._find_key(kid, algorithm)
        if key is not None:
            return key

        for _ in range(self.max_jwks_refresh):
            self._ensure_jwks(now, force=True)
            key = self._find_key(kid, algorithm)
            if key is not None:
                return key
        raise OidcTokenError("unknown key id")

    def snapshot(self):
        with self._lock:
            cache_size = len(self._jwks) if self._jwks is not None else 0
            return {
                "jwks_refreshes": self._jwks_refreshes,
                "jwks_cache_size": cache_size,
                "jwks_cache_max": self.max_jwks_keys,
                "sessions": len(self._sessions),
                "used_nonces": len(self._used_nonces),
            }
