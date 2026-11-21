"""One canonical byte stream for signing and verification."""


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
        raise NotImplementedError

    def canonicalize(self, obj):
        raise NotImplementedError

    def sign(self, obj, key_id):
        raise NotImplementedError

    def verify(self, obj, signature, key_id):
        raise NotImplementedError

    def envelope(self, obj, key_id):
        raise NotImplementedError

    def verify_envelope(self, envelope, obj=None):
        raise NotImplementedError

    def decode_signature(self, text):
        raise NotImplementedError
