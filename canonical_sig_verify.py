"""One canonical byte stream for signing and verification."""

import base64
import binascii
import hashlib
import hmac
import json
import math
import re


_BASE64_RE = re.compile(rb"[A-Za-z0-9+/]*={0,2}\Z")


class CanonicalSigVerify:
    """Canonical JSON plus HMAC signatures shared by signer and verifier.

    ``keys`` maps key ids to HMAC secret bytes.  ``version`` is the
    canonicalization version and is part of every signed payload.

    ``canonicalize(obj)`` returns UTF-8 bytes of a canonical rendering:
    keys sorted by Unicode code point, compact separators, strings emitted as
    UTF-8 (non-ASCII characters are not escaped) with the standard JSON
    escapes for control characters.  Integers render plainly; a float with an
    integral value renders without a decimal point (``1``, ``1.0`` and ``1e0``
    all become ``1``); other floats use their shortest representation.
    ``NaN`` and both infinities raise ``ValueError``.

    ``loads(text)`` parses JSON and raises ``ValueError`` for duplicate keys,
    invalid JSON, ``NaN``/``Infinity`` constants or trailing data.

    The signed bytes are ``b"v" + version + b"\n" + key_id + b"\n" +
    canonicalize(obj)``.  ``sign(obj, key_id)`` returns a standard base64
    (``+``/``/`` alphabet, ``=`` padding) HMAC-SHA256 signature.
    ``envelope(obj, key_id)`` returns ``{"canonical_version", "key_id",
    "digest", "signature"}`` where ``digest`` is the sha256 hex digest of the
    signed bytes.

    ``verify(obj, signature, key_id)`` returns bool and raises ``ValueError``
    for an unknown key id.  Only strict standard base64 is accepted:
    whitespace, URL-safe characters and missing padding make verification
    fail.  ``verify_envelope(envelope, obj=None)`` returns bool, checks the
    canonical version, the key id against the trusted key set, the recorded
    digest against a fresh canonicalization of ``obj`` and the HMAC; it
    returns False when ``obj`` is omitted.

    ``None`` values are distinct from missing keys in the canonical stream.
    ``decode_signature(text)`` raises ``ValueError`` unless ``text`` is strict
    standard base64.
    """

    def __init__(self, keys, version="1"):
        self.keys = dict(keys)
        self.version = str(version)

    def loads(self, text):
        def reject_duplicates(pairs):
            seen = set()
            for key, _ in pairs:
                if key in seen:
                    raise ValueError("duplicate key: %r" % key)
                seen.add(key)
            return dict(pairs)

        try:
            return json.loads(
                text,
                object_pairs_hook=reject_duplicates,
                parse_constant=self._reject_constant,
            )
        except ValueError:
            raise
        except (TypeError, RecursionError) as exc:
            raise ValueError(str(exc)) from exc

    @staticmethod
    def _reject_constant(value):
        raise ValueError("non-finite JSON constant: %s" % value)

    def canonicalize(self, obj):
        return self._render(obj).encode("utf-8")

    def _render(self, obj):
        if obj is None:
            return "null"
        if isinstance(obj, bool):
            return "true" if obj else "false"
        if isinstance(obj, int):
            return str(obj)
        if isinstance(obj, float):
            if not math.isfinite(obj):
                raise ValueError("non-finite floats are not canonicalizable")
            if obj.is_integer():
                return str(int(obj))
            return repr(obj)
        if isinstance(obj, str):
            return json.dumps(obj, ensure_ascii=False)
        if isinstance(obj, dict):
            parts = []
            for key in sorted(obj):
                if not isinstance(key, str):
                    raise ValueError("object keys must be strings")
                parts.append(
                    json.dumps(key, ensure_ascii=False) + ":" + self._render(obj[key])
                )
            return "{" + ",".join(parts) + "}"
        if isinstance(obj, list):
            return "[" + ",".join(self._render(item) for item in obj) + "]"
        raise ValueError("type is not canonicalizable: %r" % type(obj))

    def _signed_bytes(self, obj, key_id):
        if not isinstance(key_id, str) or key_id not in self.keys:
            raise ValueError("unknown key id: %r" % key_id)
        return (
            b"v"
            + self.version.encode("utf-8")
            + b"\n"
            + key_id.encode("utf-8")
            + b"\n"
            + self.canonicalize(obj)
        )

    def sign(self, obj, key_id):
        signed = self._signed_bytes(obj, key_id)
        digest = hmac.new(self.keys[key_id], signed, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def verify(self, obj, signature, key_id):
        try:
            provided = self.decode_signature(signature)
        except ValueError:
            return False
        if not isinstance(key_id, str) or key_id not in self.keys:
            raise ValueError("unknown key id: %r" % key_id)
        signed = self._signed_bytes(obj, key_id)
        expected = hmac.new(self.keys[key_id], signed, hashlib.sha256).digest()
        return hmac.compare_digest(expected, provided)

    def envelope(self, obj, key_id):
        signed = self._signed_bytes(obj, key_id)
        signature = base64.b64encode(
            hmac.new(self.keys[key_id], signed, hashlib.sha256).digest()
        ).decode("ascii")
        return {
            "canonical_version": self.version,
            "key_id": key_id,
            "digest": hashlib.sha256(signed).hexdigest(),
            "signature": signature,
        }

    def verify_envelope(self, envelope, obj=None):
        if obj is None:
            return False
        try:
            if not isinstance(envelope, dict):
                return False
            version = envelope["canonical_version"]
            key_id = envelope["key_id"]
            recorded_digest = envelope["digest"]
            signature = envelope["signature"]
            if version != self.version:
                return False
            if not isinstance(key_id, str) or key_id not in self.keys:
                return False
            signed = self._signed_bytes(obj, key_id)
            if not hmac.compare_digest(
                hashlib.sha256(signed).hexdigest(), str(recorded_digest)
            ):
                return False
            provided = self.decode_signature(signature)
            expected = hmac.new(
                self.keys[key_id], signed, hashlib.sha256
            ).digest()
            return hmac.compare_digest(expected, provided)
        except (KeyError, TypeError, ValueError):
            return False

    def decode_signature(self, text):
        if not isinstance(text, str) or not text:
            raise ValueError("signature must be a non-empty string")
        raw = text.encode("ascii")
        if len(raw) % 4 != 0 or not _BASE64_RE.fullmatch(raw):
            raise ValueError("signature is not strict standard base64")
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid base64 signature: %s" % exc) from exc
