class SizeLimitExceeded(Exception):
    """Raised when the directory reports a truncated result set."""


class LdapNestedGroupFold:
    """Expand nested LDAP groups into a transitive membership closure."""

    def __init__(self, source, follow_referrals=False, referral_allowlist=None,
                 max_depth=8, page_limit=100):
        raise NotImplementedError()

    def canonical_dn(self, dn):
        raise NotImplementedError()

    def members(self, dn):
        raise NotImplementedError()

    def effective_members(self, dn):
        raise NotImplementedError()

    def groups_for(self, member_dn):
        raise NotImplementedError()

    def invalidate(self, dn):
        raise NotImplementedError()

    def refresh(self, dn=None):
        raise NotImplementedError()
