import base64
import hashlib
import hmac
import json
import logging
import math
import secrets
import threading


logger = logging.getLogger(__name__)

_TOKEN_VERSION = 1
_NONCE_SIZE = 16
_MAC_SIZE = 32
_MAX_TOKEN_SIZE = 4096
_MAX_PAYLOAD_SIZE = 2048
_MAX_FIELD_SIZE = 1024

_REJECTION_REASONS = (
    "bad_token",
    "expired",
    "amplification",
    "address",
    "connection_id",
    "path_challenge",
    "replay",
    "clock",
)


def _b64encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return None
    if len(encoded) % 4 == 1:
        return None
    try:
        decoded = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError):
        return None
    if _b64encode(decoded) != value:
        return None
    return decoded


def _is_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError):
        return False


class QuicRetryGuard:
    def __init__(self, secret=b"retry-secret", max_amplification=3,
                 token_ttl=10.0, retry_prefix="c0ffee"):
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not isinstance(secret, (bytes, bytearray)):
            raise ValueError("secret must be bytes")
        if not _is_number(max_amplification) or max_amplification < 0:
            raise ValueError("max_amplification must be non-negative")
        if not _is_number(token_ttl) or token_ttl < 0:
            raise ValueError("token_ttl must be non-negative")
        if retry_prefix is None:
            retry_prefix = ""
        if isinstance(retry_prefix, bytes):
            retry_prefix = retry_prefix.decode("ascii", "ignore")
        if not isinstance(retry_prefix, str):
            raise ValueError("retry_prefix must be a string")

        self.secret = bytes(secret)
        self.max_amplification = max_amplification
        self.token_ttl = token_ttl
        self.retry_prefix = retry_prefix

        self._lock = threading.RLock()
        self._generation = 0
        self._key = self._derive_key(self._generation)
        self._last_now = None

        self._budgets = {}
        self._path_challenges = {}
        self._0rtt_tickets = {}
        self._connections = {}

        self._stats = {
            "accepted": 0,
            "retry_issued": 0,
            "migrations": 0,
            "nat_rebinds": 0,
            "path_challenges": 0,
            "path_validations": 0,
            "0rtt_accepted": 0,
            "0rtt_replays": 0,
            "0rtt_expired": 0,
            "rejected": {reason: 0 for reason in _REJECTION_REASONS},
        }

    def _derive_key(self, generation):
        context = b"quic-retry-key:" + str(generation).encode("ascii")
        return hmac.new(self.secret, context, hashlib.sha256).digest()

    def _observe_now(self, now):
        if not _is_number(now):
            raise ValueError("now must be a finite number")
        if self._last_now is not None and now < self._last_now:
            raise ValueError("monotonic clock moved backwards")
        self._last_now = now

    def _reject(self, reason, detail=None):
        rejected = self._stats["rejected"]
        if reason not in rejected:
            rejected[reason] = 0
        rejected[reason] += 1
        if detail is not None and detail != reason:
            if detail not in rejected:
                rejected[detail] = 0
            rejected[detail] += 1
        logger.warning(
            "quic retry rejection reason=%s detail=%s", reason, detail
        )

    def rotate_key(self):
        with self._lock:
            self._generation += 1
            self._key = self._derive_key(self._generation)

    def _encode_token(self, connection_id, address, issued_at):
        payload = json.dumps(
            {
                "v": _TOKEN_VERSION,
                "g": self._generation,
                "t": issued_at,
                "a": address,
                "c": connection_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        for _ in range(64):
            nonce = secrets.token_bytes(_NONCE_SIZE)
            mac = hmac.new(self._key, nonce + payload, hashlib.sha256).digest()
            token = ".".join(
                (_b64encode(nonce), _b64encode(payload), _b64encode(mac))
            )
            if not self.retry_prefix:
                return token
            if len(self.retry_prefix) >= 4:
                if self.retry_prefix not in token:
                    return token
            elif not token.startswith(self.retry_prefix):
                return token
        raise RuntimeError("could not generate a token without the static prefix")

    def _new_challenge(self):
        for _ in range(64):
            challenge = secrets.token_urlsafe(32)
            if not self.retry_prefix:
                return challenge
            if len(self.retry_prefix) >= 4:
                if self.retry_prefix not in challenge:
                    return challenge
            elif not challenge.startswith(self.retry_prefix):
                return challenge
        raise RuntimeError("could not generate a challenge without the static prefix")

    def _decode_token(self, token):
        if not isinstance(token, str):
            return None
        try:
            encoded = token.encode("ascii")
        except UnicodeEncodeError:
            return None
        if not encoded or len(encoded) > _MAX_TOKEN_SIZE:
            return None

        parts = token.split(".")
        if len(parts) != 3:
            return None
        nonce = _b64decode(parts[0])
        payload = _b64decode(parts[1])
        mac = _b64decode(parts[2])
        if nonce is None or payload is None or mac is None:
            return None
        if len(nonce) != _NONCE_SIZE or len(mac) != _MAC_SIZE:
            return None
        if not payload or len(payload) > _MAX_PAYLOAD_SIZE:
            return None

        expected = hmac.new(self._key, nonce + payload, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            return None

        try:
            claims = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(claims, dict):
            return None
        if set(claims) != {"v", "g", "t", "a", "c"}:
            return None
        if claims["v"] != _TOKEN_VERSION:
            return None
        if (
            not isinstance(claims["g"], int)
            or isinstance(claims["g"], bool)
            or claims["g"] < 0
        ):
            return None
        if not _is_number(claims["t"]):
            return None
        if (
            not isinstance(claims["a"], str)
            or len(claims["a"]) > _MAX_FIELD_SIZE
            or not isinstance(claims["c"], str)
            or len(claims["c"]) > _MAX_FIELD_SIZE
        ):
            return None
        return claims

    def _check_claims(self, claims, address, connection_id, now):
        if claims is None:
            return "bad_token"
        if claims["g"] != self._generation:
            return "bad_token"
        if claims["a"] != address:
            return "address"
        if claims["c"] != connection_id:
            return "connection_id"
        if now < claims["t"]:
            return "clock"
        if now - claims["t"] > self.token_ttl:
            return "expired"
        return None

    def issue_retry(self, connection_id, address, now):
        with self._lock:
            if not isinstance(connection_id, str) or not connection_id:
                raise ValueError("connection_id must be a non-empty string")
            if not isinstance(address, str) or not address:
                raise ValueError("address must be a non-empty string")
            self._observe_now(now)
            token = self._encode_token(connection_id, address, now)
            self._stats["retry_issued"] += 1
            return {
                "token": token,
                "issued_at": now,
                "expires_at": now + self.token_ttl,
            }

    def validate_token(self, token, address, connection_id, now):
        with self._lock:
            claims = self._decode_token(token)
            if claims is None:
                return False
            self._observe_now(now)
            return self._check_claims(
                claims, address, connection_id, now
            ) is None

    def _receive(self, address, size):
        budget = size * self.max_amplification
        self._budgets[address] = self._budgets.get(address, 0) + budget

    def check_initial(self, connection_id, address, size, now, token=None):
        with self._lock:
            if not isinstance(connection_id, str) or not connection_id:
                raise ValueError("connection_id must be a non-empty string")
            if not isinstance(address, str) or not address:
                raise ValueError("address must be a non-empty string")
            if not _is_number(size) or size < 0:
                raise ValueError("size must be a non-negative finite number")
            self._observe_now(now)
            self._receive(address, size)

            if token is None:
                issued = self._encode_token(connection_id, address, now)
                self._stats["retry_issued"] += 1
                return {"action": "retry", "token": issued, "reason": "no_token"}

            claims = self._decode_token(token)
            reason = self._check_claims(
                claims, address, connection_id, now
            )
            if reason is None:
                self._stats["accepted"] += 1
                return {"action": "accept"}

            self._reject("bad_token" if reason in {
                "bad_token", "address", "connection_id", "clock"
            } else reason, detail=reason)
            issued = self._encode_token(connection_id, address, now)
            self._stats["retry_issued"] += 1
            return {"action": "retry", "token": issued, "reason": reason}

    def send_budget(self, address):
        with self._lock:
            return self._budgets.get(address, 0)

    def charge_send(self, address, size):
        with self._lock:
            if not isinstance(address, str) or not address:
                raise ValueError("address must be a non-empty string")
            if not _is_number(size) or size < 0:
                raise ValueError("size must be a non-negative finite number")
            remaining = self._budgets.get(address, 0)
            if size > remaining:
                self._reject("amplification")
                raise ValueError("amplification budget exceeded")
            self._budgets[address] = remaining - size
            return self._budgets[address]

    @staticmethod
    def _address_parts(address):
        if not isinstance(address, str) or not address:
            raise ValueError("address must be a non-empty string")
        if address.startswith("["):
            closing = address.find("]")
            if closing < 0 or closing + 1 >= len(address):
                raise ValueError("invalid address")
            host = address[1:closing]
            if address[closing + 1] != ":":
                raise ValueError("invalid address")
            port_text = address[closing + 2:]
        else:
            if ":" not in address:
                raise ValueError("address must include a port")
            host, port_text = address.rsplit(":", 1)
        if not host or not port_text.isdigit():
            raise ValueError("invalid address")
        port = int(port_text)
        if port < 0 or port > 65535:
            raise ValueError("invalid port")
        return host.lower(), port

    def on_migration(self, connection_id, old_address, new_address, now):
        with self._lock:
            if not isinstance(connection_id, str) or not connection_id:
                raise ValueError("connection_id must be a non-empty string")
            old_host, old_port = self._address_parts(old_address)
            new_host, new_port = self._address_parts(new_address)
            self._observe_now(now)

            if old_host == new_host and old_port == new_port:
                return {"migrated": False, "challenge": None, "reason": "unchanged"}

            if old_host == new_host:
                self._connections[connection_id] = {"address": new_address}
                self._stats["nat_rebinds"] += 1
                return {"migrated": False, "challenge": None, "reason": "nat_rebind"}

            challenge = self._new_challenge()
            self._path_challenges[connection_id] = {
                "challenge": challenge,
                "address": new_address,
                "old_address": old_address,
                "issued_at": now,
            }
            self._connections[connection_id] = {"address": old_address}
            self._stats["migrations"] += 1
            self._stats["path_challenges"] += 1
            return {
                "migrated": True,
                "challenge": challenge,
                "reason": "path_challenge_required",
            }

    def validate_path_challenge(self, connection_id, challenge, address, now):
        with self._lock:
            if (
                not isinstance(connection_id, str)
                or not connection_id
                or not isinstance(challenge, str)
                or not challenge
                or not isinstance(address, str)
                or not address
            ):
                return False
            pending = self._path_challenges.get(connection_id)
            if pending is None:
                self._reject("path_challenge")
                return False
            self._observe_now(now)
            if (
                pending["challenge"] != challenge
                or pending["address"] != address
            ):
                self._reject("path_challenge")
                return False
            if now < pending["issued_at"]:
                self._reject("clock")
                return False
            if now - pending["issued_at"] > self.token_ttl:
                self._path_challenges.pop(connection_id, None)
                self._reject("expired")
                return False

            self._path_challenges.pop(connection_id, None)
            self._connections[connection_id] = {"address": address}
            self._stats["path_validations"] += 1
            return True

    def on_0rtt(self, connection_id, ticket, now):
        with self._lock:
            if not isinstance(connection_id, str) or not connection_id:
                raise ValueError("connection_id must be a non-empty string")
            if not isinstance(ticket, str) or not ticket:
                return {"accepted": False, "reason": "bad_ticket"}
            if len(ticket) > _MAX_FIELD_SIZE:
                return {"accepted": False, "reason": "bad_ticket"}
            self._observe_now(now)

            first_use = self._0rtt_tickets.get(ticket)
            if first_use is None:
                self._0rtt_tickets[ticket] = now
                self._stats["0rtt_accepted"] += 1
                return {"accepted": True}

            if now - first_use > self.token_ttl:
                self._stats["0rtt_expired"] += 1
                self._reject("expired")
                return {"accepted": False, "reason": "expired"}

            self._stats["0rtt_replays"] += 1
            self._reject("replay")
            return {"accepted": False, "reason": "replay"}

    def snapshot(self):
        with self._lock:
            return {
                "accepted": self._stats["accepted"],
                "retry_issued": self._stats["retry_issued"],
                "migrations": self._stats["migrations"],
                "nat_rebinds": self._stats["nat_rebinds"],
                "path_challenges": self._stats["path_challenges"],
                "path_validations": self._stats["path_validations"],
                "0rtt_accepted": self._stats["0rtt_accepted"],
                "0rtt_replays": self._stats["0rtt_replays"],
                "0rtt_expired": self._stats["0rtt_expired"],
                "rejected": dict(self._stats["rejected"]),
            }
