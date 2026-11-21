"""POSIX ACL effective-permission evaluation."""


class PosixAclEffective:
    """Evaluate POSIX access ACLs in the order the kernel uses.

    An ACL is a list of ``{"tag", "id", "perms"}`` dicts.  ``tag`` is one of
    ``"user"``, ``"group"``, ``"mask"`` or ``"other"``; ``id`` is ``None``
    for the owner / owning-group / mask / other entries and a name for a named
    user or named group entry.  ``perms`` is a symbolic string such as
    ``"r-x"``; ``-`` placeholders are ignored and the canonical result
    contains only the granted letters in ``rwx`` order (the empty string is
    allowed).

    ``normalize(acl)`` validates entries, merges duplicate ``(tag, id)``
    entries by intersecting their permissions (the most restrictive wins),
    and returns a new list ordered ``user::``, named users by id, ``group::``,
    named groups by id, ``mask::``, ``other::``.  Input order never matters.

    ``classify(acl)`` returns ``"minimal"`` when the ACL is exactly the three
    base entries (owner, owning group, other) and ``"extended"`` otherwise
    (a mask or any named entry makes it extended).

    ``effective(acl, subject, owner=None, owner_group=None, groups=())``
    returns ``{"perms", "matched", "mask"}``.  The deciding entry is chosen in
    this order:

      1. the owner entry when ``subject == owner`` (never masked);
      2. a named user entry for ``subject`` (masked);
      3. the owning group entry when ``owner_group`` is in ``groups``
         (masked);
      4. the first named group entry, in normalized order, whose id is in
         ``groups`` (masked);
      5. the other entry (never masked).

    The mask is only applied to named-user and group entries.  ``matched`` is
    a copy of the deciding entry and ``mask`` is the mask permissions or None.

    ``check(...)`` accepts the same arguments plus ``wanted`` (a permission
    string) and ``path_kind``; it returns True only when every wanted
    permission is present in the effective permissions.  Directory search
    permission is therefore checked separately from read permission.

    ``inherit(default_acl, umask, mode=0o666, is_dir=False)`` returns None
    when there is no default ACL.  Otherwise it builds the access ACL of a new
    object: ``user::`` and ``other::`` come from ``mode``, a mask entry is
    intersected with the ``mode`` group bits (or ``group::`` is set to those
    bits for a minimal ACL), named entries are copied unchanged and ``umask``
    is ignored because the default ACL already encodes the policy.

    ``apply_chmod(acl, mode)`` applies chmod semantics: ``user::`` and
    ``other::`` take the owner and other bits of ``mode``; when a mask exists
    the mask takes the group bits and ``group::`` is left untouched, otherwise
    ``group::`` takes the group bits.

    ``apply_tree(entries, mode)`` processes ``{"path", "acl", "symlink"}``
    records.  Regular entries are chmodded themselves.  A symlink entry is
    chmodded on its target (``symlink`` names another record's path) when the
    policy is ``"follow"``; with ``"reject"`` it raises ``PermissionError``.
    The returned dict maps every path to its resulting ACL.
    """

    def __init__(self, symlink_policy="follow"):
        self.symlink_policy = symlink_policy

    def normalize(self, acl):
        raise NotImplementedError

    def classify(self, acl):
        raise NotImplementedError

    def effective(self, acl, subject, owner=None, owner_group=None, groups=()):
        raise NotImplementedError

    def check(self, acl, subject, owner=None, owner_group=None, groups=(),
              wanted="r", path_kind="file"):
        raise NotImplementedError

    def inherit(self, default_acl, umask, mode=0o666, is_dir=False):
        raise NotImplementedError

    def apply_chmod(self, acl, mode):
        raise NotImplementedError

    def apply_tree(self, entries, mode):
        raise NotImplementedError
