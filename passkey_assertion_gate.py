"""WebAuthn passkey registration and assertion validation gate."""


class PasskeyAssertionGate:
    """Validates registration and assertion responses for passkey login."""

    def __init__(self, rp_id, allowed_origins, verifier=None, uv_required=True,
                 challenge_ttl=300, allowed_algorithms=("ES256", "RS256")):
        raise NotImplementedError()

    def register_begin(self, session_id, user_id, action="register"):
        raise NotImplementedError()

    def register_finish(self, session_id, response, now):
        raise NotImplementedError()

    def assert_begin(self, session_id, user_id, action="login",
                     allow_credentials=None):
        raise NotImplementedError()

    def assert_finish(self, session_id, response, now):
        raise NotImplementedError()

    def credentials(self, user_id):
        raise NotImplementedError()
