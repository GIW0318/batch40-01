"""Admission and revocation cache for access tokens."""


class TokenRevocationCache:
    """Caches token admission decisions and tracks revocations per issuer."""

    def __init__(self, negative_limit=1000, positive_limit=1000):
        raise NotImplementedError()

    def check(self, issuer, token_id, subject, issued_at, expires_at, now):
        raise NotImplementedError()

    def revoke_token(self, issuer, token_id, now, reason="logout"):
        raise NotImplementedError()

    def revoke_account(self, issuer, subject, cutoff, reason="account_banned"):
        raise NotImplementedError()

    def apply_revocations(self, events, now):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
