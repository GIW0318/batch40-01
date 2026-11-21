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
        if symlink_policy not in ("follow", "reject"):
            raise ValueError("symlink_policy must be 'follow' or 'reject'")
        self.symlink_policy = symlink_policy

    _BASE_TAGS = ("user", "group", "mask", "other")
    _PERM_BITS = {"r": 4, "w": 2, "x": 1}

    @staticmethod
    def _perms_to_int(perms):
        value = 0
        for char in perms:
            if char in PosixAclEffective._PERM_BITS:
                value |= PosixAclEffective._PERM_BITS[char]
        return value

    @staticmethod
    def _int_to_perms(value):
        letters = []
        for letter, bit in (("r", 4), ("w", 2), ("x", 1)):
            if value & bit:
                letters.append(letter)
        return "".join(letters)

    @classmethod
    def _canonical_perms(cls, perms):
        if not isinstance(perms, str):
            raise ValueError("entry perms must be a string: %r" % (perms,))
        invalid = set(perms) - set("rwx-")
        if invalid:
            raise ValueError("invalid permission letters: %r" % (perms,))
        return cls._int_to_perms(cls._perms_to_int(perms))

    @staticmethod
    def _validate_entry(entry):
        if not isinstance(entry, dict):
            raise ValueError("ACL entry must be a dict: %r" % (entry,))
        tag = entry.get("tag")
        if tag not in ("user", "group", "mask", "other"):
            raise ValueError("unknown ACL tag: %r" % (tag,))
        ident = entry.get("id")
        if tag in ("mask", "other") and ident is not None:
            raise ValueError("%s entries must have id None" % tag)
        if tag in ("user", "group") and ident is not None and not isinstance(
            ident, str
        ):
            raise ValueError("named entry ids must be strings: %r" % (ident,))
        PosixAclEffective._canonical_perms(entry.get("perms", ""))

    @staticmethod
    def _sort_key(entry):
        tag, ident = entry["tag"], entry["id"]
        if tag == "user":
            return (0, 0, "") if ident is None else (0, 1, ident)
        if tag == "group":
            return (1, 0, "") if ident is None else (1, 1, ident)
        if tag == "mask":
            return (2, 0, "")
        return (3, 0, "")

    def normalize(self, acl):
        merged = {}
        order = []
        for entry in acl:
            self._validate_entry(entry)
            key = (entry["tag"], entry["id"])
            perms = self._perms_to_int(entry["perms"])
            if key in merged:
                merged[key] &= perms
            else:
                merged[key] = perms
                order.append(key)
        normalized = [
            {
                "tag": tag,
                "id": ident,
                "perms": self._int_to_perms(merged[(tag, ident)]),
            }
            for tag, ident in order
        ]
        normalized.sort(key=self._sort_key)
        return normalized

    @staticmethod
    def _find(acl, tag, ident):
        for entry in acl:
            if entry["tag"] == tag and entry["id"] == ident:
                return entry
        return None

    def classify(self, acl):
        for entry in acl:
            if entry["tag"] == "mask" or entry["id"] is not None:
                return "extended"
        return "minimal"

    def effective(self, acl, subject, owner=None, owner_group=None, groups=()):
        acl = self.normalize(acl)
        group_set = set(groups)
        mask_entry = self._find(acl, "mask", None)
        mask_value = (
            self._perms_to_int(mask_entry["perms"]) if mask_entry else None
        )

        matched = None
        masked = False
        if owner is not None and subject == owner:
            matched = self._find(acl, "user", None)
        else:
            named_user = self._find(acl, "user", subject)
            if named_user is not None:
                matched = named_user
                masked = True
            elif owner_group is not None and owner_group in group_set:
                matched = self._find(acl, "group", None)
                masked = True
            else:
                for entry in acl:
                    if (
                        entry["tag"] == "group"
                        and entry["id"] is not None
                        and entry["id"] in group_set
                    ):
                        matched = entry
                        masked = True
                        break
                if matched is None:
                    matched = self._find(acl, "other", None)

        if matched is None:
            raise ValueError("ACL has no entry matching subject %r" % (subject,))

        value = self._perms_to_int(matched["perms"])
        if masked and mask_value is not None:
            value &= mask_value
        return {
            "perms": self._int_to_perms(value),
            "matched": dict(matched),
            "mask": (
                self._int_to_perms(mask_value)
                if mask_value is not None
                else None
            ),
        }

    def check(self, acl, subject, owner=None, owner_group=None, groups=(),
              wanted="r", path_kind="file"):
        if path_kind not in ("file", "dir"):
            raise ValueError("path_kind must be 'file' or 'dir'")
        result = self.effective(
            acl, subject, owner=owner, owner_group=owner_group, groups=groups
        )
        granted = self._perms_to_int(result["perms"])
        return (self._perms_to_int(wanted) & ~granted) == 0

    def inherit(self, default_acl, umask, mode=0o666, is_dir=False):
        if not default_acl:
            return None
        default_acl = self.normalize(default_acl)
        owner_bits = (mode >> 6) & 7
        group_bits = (mode >> 3) & 7
        other_bits = mode & 7

        inherited = []
        for entry in default_acl:
            tag, ident = entry["tag"], entry["id"]
            if tag == "user" and ident is None:
                perms = self._int_to_perms(
                    self._perms_to_int(entry["perms"]) & owner_bits
                )
            elif tag == "other":
                perms = self._int_to_perms(
                    self._perms_to_int(entry["perms"]) & other_bits
                )
            elif tag == "mask":
                perms = self._int_to_perms(
                    self._perms_to_int(entry["perms"]) & group_bits
                )
            else:
                perms = entry["perms"]
            inherited.append({"tag": tag, "id": ident, "perms": perms})

        if self._find(inherited, "mask", None) is None:
            owning_group = self._find(inherited, "group", None)
            if owning_group is not None:
                owning_group["perms"] = self._int_to_perms(
                    self._perms_to_int(owning_group["perms"]) & group_bits
                )
        return inherited

    def apply_chmod(self, acl, mode):
        updated = self.normalize(acl)
        owner_perms = self._int_to_perms((mode >> 6) & 7)
        group_perms = self._int_to_perms((mode >> 3) & 7)
        other_perms = self._int_to_perms(mode & 7)

        owner_entry = self._find(updated, "user", None)
        if owner_entry is not None:
            owner_entry["perms"] = owner_perms
        other_entry = self._find(updated, "other", None)
        if other_entry is not None:
            other_entry["perms"] = other_perms

        mask_entry = self._find(updated, "mask", None)
        if mask_entry is not None:
            mask_entry["perms"] = group_perms
        else:
            group_entry = self._find(updated, "group", None)
            if group_entry is not None:
                group_entry["perms"] = group_perms
        return updated

    def apply_tree(self, entries, mode):
        records = {entry["path"]: entry for entry in entries}
        result = {}
        for entry in entries:
            path = entry["path"]
            target = entry.get("symlink")
            if target:
                if self.symlink_policy == "reject":
                    raise PermissionError(
                        "symlink %s -> %s rejected by policy" % (path, target)
                    )
                target_entry = records.get(target)
                if target_entry is None:
                    raise PermissionError(
                        "symlink %s target %s not found" % (path, target)
                    )
                result[path] = self.normalize(entry["acl"])
                result[target] = self.apply_chmod(target_entry["acl"], mode)
            else:
                result[path] = self.apply_chmod(entry["acl"], mode)
        return result
