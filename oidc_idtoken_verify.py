"""OIDC id_token signature and claim verification."""


class OidcTokenError(Exception):
    """Uniform external error for every invalid id_token."""

    code = "invalid_token"

    def __init__(self, reason=""):
        super().__init__("invalid_token")
        self.reason = reason
        self.code = "invalid_token"


class OidcIdTokenVerify:
    """Verifies compact JWT id_tokens against JWKS and OIDC claim rules."""

    def __init__(self, issuer, client_id, jwks_provider, verifier=None,
                 allowed_algorithms=("RS256", "ES256"), clock_skew=60,
                 max_jwks_refresh=2):
        raise NotImplementedError()

    def begin(self, session_id, nonce=None):
        raise NotImplementedError()

    def verify(self, token, session_id, access_token=None, code=None, now=0):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
