"""Path policy for a self-service secrets store."""


class SecretsPathPolicy:
    """Normalize request paths and authorize read/list actions.

    ``rules`` is a list of ``{"effect": "allow"|"deny", "subjects": [...],
    "actions": [...], "path": "secret/data/${app}/*"}``.  A rule may use
    ``"subject"`` instead of ``"subjects"``.

    Requests are normalized before matching: backslashes become ``/``,
    percent escapes are decoded exactly once, empty and ``.`` segments are
    dropped and ``..`` is resolved; a path that would escape the root is
    rejected as ``bad_path``.  The empty path is the root and is refused with
    ``root_path`` unless a rule's normalized pattern is also empty.

    ``*`` matches exactly one non-empty segment and never crosses ``/``.
    There is no implicit subtree wildcard.  Matching is case-sensitive unless
    the policy was built with ``case_sensitive=False``, which lower-cases both
    sides.  ``${name}`` placeholders in subjects and paths are expanded from
    ``variables``; when a variable is missing the rule simply does not match
    (default deny).

    Deny rules are evaluated after every match and always beat allow rules,
    regardless of rule order.

    ``authorize(subject, action, path, lease_id=None, credential=None)``
    returns ``{"allowed", "reason", "path", "rule"}``.  ``rule`` is the index
    of the deciding rule or None.  Reasons are ``"allowed"``, ``"denied"``,
    ``"no_match"``, ``"root_path"``, ``"bad_path"`` and ``"lease_revoked"``.
    ``read`` and ``list`` are separate actions.

    ``issue_lease(subject, actions, paths, ttl=3600)`` returns a lease id,
    ``derive_credential(lease_id)`` returns a token bound to that lease and
    ``revoke_lease(lease_id)`` invalidates both the lease and every derived
    credential.  A request presenting a revoked lease or credential is denied
    with ``"lease_revoked"`` before rule matching.

    ``snapshot()`` returns ``{"rule_count", "digest", "case_sensitive"}`` and
    never the internal rule objects.
    """

    def __init__(self, rules, case_sensitive=True, variables=None):
        self.rules = list(rules)
        self.case_sensitive = case_sensitive
        self.variables = dict(variables or {})

    def authorize(self, subject, action, path, lease_id=None, credential=None):
        raise NotImplementedError

    def issue_lease(self, subject, actions, paths, ttl=3600):
        raise NotImplementedError

    def derive_credential(self, lease_id):
        raise NotImplementedError

    def revoke_lease(self, lease_id):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
