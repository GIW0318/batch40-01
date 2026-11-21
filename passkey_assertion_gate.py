"""WebAuthn passkey registration and assertion validation gate."""

import hashlib
import json
import secrets

_UP_FLAG = 0x01
_UV_FLAG = 0x04
_MIN_AUTH_DATA_LEN = 32 + 1 + 4


class PasskeyAssertionGate:
    """Validates registration and assertion responses for passkey login."""

    def __init__(self, rp_id, allowed_origins, verifier=None, uv_required=True,
                 challenge_ttl=300, allowed_algorithms=("ES256", "RS256")):
        self.rp_id = rp_id
        self.allowed_origins = tuple(allowed_origins)
        self.verifier = verifier
        self.uv_required = uv_required
        self.challenge_ttl = challenge_ttl
        self.allowed_algorithms = frozenset(allowed_algorithms)
        self._rp_id_hash = hashlib.sha256(rp_id.encode()).digest()
        self._challenges = {}
        self._credentials = {}
        self._user_credentials = {}

    def register_begin(self, session_id, user_id, action="register", now=0):
        return self._begin(session_id, user_id, "register", action, now, None)

    def register_finish(self, session_id, response, now):
        record = self._consume_challenge(session_id, response, "register", now)
        self._check_client_data(response, "webauthn.create")
        counter = self._parse_authenticator_data(response.get("authenticator_data"))
        algorithm = response.get("algorithm")
        if algorithm not in self.allowed_algorithms:
            raise ValueError("algorithm is not allowed")
        credential_id = response.get("credential_id")
        if not credential_id:
            raise ValueError("missing credential_id")
        public_key = response.get("public_key")
        if not public_key:
            raise ValueError("missing public_key")
        self._verify_signature(public_key, algorithm,
                               response["authenticator_data"],
                               response["client_data_json"],
                               response.get("signature"))
        user_id = record["user_id"]
        self._credentials[credential_id] = {
            "credential_id": credential_id,
            "public_key": public_key,
            "algorithm": algorithm,
            "user_id": user_id,
            "user_handle": response.get("user_handle"),
            "counter": counter,
        }
        self._user_credentials.setdefault(user_id, set()).add(credential_id)
        return {"ok": True, "credential_id": credential_id, "user_id": user_id}

    def assert_begin(self, session_id, user_id, action="login",
                     allow_credentials=None, now=0):
        owned = self._user_credentials.get(user_id, set())
        if allow_credentials is None:
            allowed = set(owned)
            if not allowed:
                raise ValueError("user has no registered credentials")
        else:
            if not allow_credentials:
                raise ValueError("allow_credentials must not be empty")
            allowed = set()
            for credential_id in allow_credentials:
                if credential_id not in owned:
                    raise ValueError(
                        "allow_credentials contains a credential not owned "
                        "by the user")
                allowed.add(credential_id)
        return self._begin(session_id, user_id, "assert", action, now, allowed)

    def assert_finish(self, session_id, response, now):
        record = self._consume_challenge(session_id, response, "assert", now)
        self._check_client_data(response, "webauthn.get")
        counter = self._parse_authenticator_data(response.get("authenticator_data"))
        credential_id = response.get("credential_id")
        credential = self._credentials.get(credential_id)
        if credential is None:
            raise ValueError("unknown credential")
        allowed = record["allow_credentials"]
        if allowed is not None and credential_id not in allowed:
            raise ValueError("credential is not in the allow list")
        if credential["user_id"] != record["user_id"]:
            raise ValueError("credential does not belong to this user")
        if response.get("user_handle") != credential["user_handle"]:
            raise ValueError("user handle does not match the credential owner")
        if response.get("algorithm") != credential["algorithm"]:
            raise ValueError("algorithm does not match the credential")
        if counter <= credential["counter"]:
            raise ValueError("signature counter did not increase")
        self._verify_signature(credential["public_key"], credential["algorithm"],
                               response["authenticator_data"],
                               response["client_data_json"],
                               response.get("signature"))
        credential["counter"] = counter
        return {"ok": True, "user_id": credential["user_id"],
                "credential_id": credential_id}

    def credentials(self, user_id):
        return [dict(self._credentials[credential_id])
                for credential_id in self._user_credentials.get(user_id, ())]

    def _begin(self, session_id, user_id, kind, action, now, allow_credentials):
        challenge = secrets.token_urlsafe(32)
        self._challenges[challenge] = {
            "session_id": session_id,
            "user_id": user_id,
            "kind": kind,
            "action": action,
            "issued_at": now,
            "allow_credentials": allow_credentials,
        }
        return {"challenge": challenge, "rp_id": self.rp_id,
                "user_id": user_id, "action": action}

    def _consume_challenge(self, session_id, response, kind, now):
        challenge = response.get("challenge")
        record = self._challenges.pop(challenge, None)
        if record is None:
            raise ValueError("unknown or already consumed challenge")
        if record["session_id"] != session_id:
            raise ValueError("challenge is bound to a different session")
        if record["kind"] != kind:
            raise ValueError("challenge was issued for a different ceremony")
        if now < record["issued_at"] or \
                now - record["issued_at"] > self.challenge_ttl:
            raise ValueError("challenge expired")
        return record

    def _check_client_data(self, response, expected_type):
        client_data_json = response.get("client_data_json")
        try:
            client_data = json.loads(client_data_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid client_data_json") from exc
        if client_data.get("type") != expected_type:
            raise ValueError("unexpected client data type")
        if client_data.get("challenge") != response.get("challenge"):
            raise ValueError("challenge mismatch in client data")
        if client_data.get("origin") not in self.allowed_origins:
            raise ValueError("origin is not allowed")

    def _parse_authenticator_data(self, authenticator_data):
        if not isinstance(authenticator_data, (bytes, bytearray)) or \
                len(authenticator_data) < _MIN_AUTH_DATA_LEN:
            raise ValueError("authenticator_data is too short")
        if bytes(authenticator_data[:32]) != self._rp_id_hash:
            raise ValueError("rp_id hash mismatch")
        flags = authenticator_data[32]
        if not flags & _UP_FLAG:
            raise ValueError("user presence flag is not set")
        if self.uv_required and not flags & _UV_FLAG:
            raise ValueError("user verification flag is not set")
        return int.from_bytes(authenticator_data[33:37], "big")

    def _verify_signature(self, public_key, algorithm, authenticator_data,
                          client_data_json, signature):
        if algorithm not in self.allowed_algorithms:
            raise ValueError("algorithm is not allowed")
        if self.verifier is None:
            return
        message = (bytes(authenticator_data)
                   + hashlib.sha256(client_data_json).digest())
        if not self.verifier(public_key, message, signature, algorithm):
            raise ValueError("signature verification failed")
