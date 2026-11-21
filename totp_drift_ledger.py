"""Multi-tenant TOTP drift compensation and consumption ledger."""


class TotpDriftLedger:
    """Verifies TOTP codes with per-tenant windows and shared failure counts."""

    def __init__(self, tenants, store=None):
        raise NotImplementedError()

    def enroll(self, tenant, user, secret, digits=6, authenticator_id=None):
        raise NotImplementedError()

    def rotate(self, tenant, user, authenticator_id, new_secret, overlap_until):
        raise NotImplementedError()

    def unbind(self, tenant, user, authenticator_id):
        raise NotImplementedError()

    def verify(self, tenant, user, code, now):
        raise NotImplementedError()

    def audit(self):
        raise NotImplementedError()
