"""Validate user-namespace id mappings before they are written to the kernel."""


class UsernsIdmapCheck:
    """Validate a complete userns id map specification.

    ``spec`` is a dict with these keys:

      ``owner``       - name of the user requesting the namespace
      ``uid_map``     - list of ``[container_start, host_start, length]``
      ``gid_map``     - list of ``[container_start, host_start, length]``
      ``setgroups``   - ``"allow"`` or ``"deny"``
      ``subordinate`` - optional ``{owner: [[host_start, length], ...]}``

    ``validate`` returns ``{"ok": bool, "issues": [...], "uid_map": [...],
    "gid_map": [...], "extras": [...]}``.  Every issue is a dict
    ``{"code": str, "detail": str}``.  Returned maps are normalized copies,
    sorted by ``container_start`` and rendered as three element lists.

    Rules:

      * a missing or ``None`` ``gid_map`` reports ``missing_gid_map``;
      * an empty uid or gid map reports ``empty_map`` (empty is not a
        shortcut for the default mapping);
      * a map that is not sorted by ``container_start`` reports ``unsorted``
        instead of being silently reordered;
      * ranges that overlap inside one map report ``overlap``;
      * a host id of 0 reports ``host_root`` unless the checker was built
        with ``allow_host_root=True``;
      * a range whose start plus length exceeds ``id_limit`` reports
        ``overflow``; negative values or non-integer fields report
        ``bad_entry``;
      * when a gid map is present, ``setgroups`` must be ``"deny"`` or
        ``setgroups_enabled`` is reported;
      * two different owners whose subordinate ranges overlap report
        ``subordinate_overlap``;
      * more than ``max_lines`` entries report ``too_many_lines``; the
        entries beyond the limit are returned in ``extras``.

    ``validate_helper_args`` applies the newuidmap/newgidmap argument checks
    to a bare list of lines and returns ``{"ok", "issues", "entries"}``.
    Each line has exactly three integers, all values non-negative, every
    length at least 1, no start plus length above ``id_limit`` and no more
    than ``max_lines`` lines (``empty_map`` for an empty list,
    ``bad_entry`` for a malformed line, ``overflow`` for a wrapped range,
    ``too_many_lines`` for excess lines).
    """

    def __init__(self, max_lines=5, id_limit=2 ** 32, allow_host_root=False):
        self.max_lines = max_lines
        self.id_limit = id_limit
        self.allow_host_root = allow_host_root

    def validate(self, spec):
        raise NotImplementedError

    def validate_helper_args(self, lines):
        raise NotImplementedError
