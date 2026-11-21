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

    @staticmethod
    def _is_int(value):
        return isinstance(value, int) and not isinstance(value, bool)

    @staticmethod
    def _issue(issues, code, detail):
        issues.append({"code": code, "detail": detail})

    def _parse_entry(self, entry):
        """Return ``[container_start, host_start, length]`` or ``None``."""
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            return None
        if not all(self._is_int(value) for value in entry):
            return None
        container_start, host_start, length = (int(value) for value in entry)
        if container_start < 0 or host_start < 0 or length < 1:
            return None
        return [container_start, host_start, length]

    def _check_map(self, raw_entries, name, issues, extras):
        """Validate one map, return entries sorted by container start."""
        entries = []
        for raw in raw_entries:
            parsed = self._parse_entry(raw)
            if parsed is None:
                self._issue(issues, "bad_entry",
                            "%s entry %r is not three non-negative integers "
                            "with a positive length" % (name, raw))
                continue
            entries.append(parsed)

        starts = [entry[0] for entry in entries]
        if starts != sorted(starts):
            self._issue(issues, "unsorted",
                        "%s is not sorted by container start" % name)

        entries.sort(key=lambda entry: entry[0])

        for entry in entries:
            container_start, host_start, length = entry
            if container_start + length > self.id_limit:
                self._issue(issues, "overflow",
                            "%s range %r wraps past the id limit" % (name, entry))
            elif host_start + length > self.id_limit:
                self._issue(issues, "overflow",
                            "%s range %r wraps past the id limit" % (name, entry))
            if host_start == 0 and not self.allow_host_root:
                self._issue(issues, "host_root",
                            "%s range %r maps to host id 0" % (name, entry))

        for index in range(1, len(entries)):
            prev, curr = entries[index - 1], entries[index]
            if curr[0] < prev[0] + prev[2]:
                self._issue(issues, "overlap",
                            "%s container ranges %r and %r overlap"
                            % (name, prev, curr))
        by_host = sorted(entries, key=lambda entry: entry[1])
        for index in range(1, len(by_host)):
            prev, curr = by_host[index - 1], by_host[index]
            if curr[1] < prev[1] + prev[2]:
                self._issue(issues, "overlap",
                            "%s host ranges %r and %r overlap"
                            % (name, prev, curr))

        if len(entries) > self.max_lines:
            self._issue(issues, "too_many_lines",
                        "%s has %d entries, limit is %d"
                        % (name, len(entries), self.max_lines))
            extras.extend(entries[self.max_lines:])

        return entries

    def validate(self, spec):
        issues = []
        extras = []

        uid_raw = spec.get("uid_map")
        if not uid_raw:
            self._issue(issues, "empty_map",
                        "uid_map is empty; empty is not the default mapping")
            uid_map = []
        else:
            uid_map = self._check_map(uid_raw, "uid_map", issues, extras)

        gid_raw = spec.get("gid_map")
        if gid_raw is None:
            self._issue(issues, "missing_gid_map",
                        "gid_map is missing; uid and gid maps must be paired")
            gid_map = []
        elif not gid_raw:
            self._issue(issues, "empty_map",
                        "gid_map is empty; empty is not the default mapping")
            gid_map = []
        else:
            gid_map = self._check_map(gid_raw, "gid_map", issues, extras)
            if spec.get("setgroups") != "deny":
                self._issue(issues, "setgroups_enabled",
                            "setgroups must be denied before writing gid_map")

        subordinate = spec.get("subordinate") or {}
        owner_ranges = []
        for owner, ranges in subordinate.items():
            for item in ranges or []:
                if (isinstance(item, (list, tuple)) and len(item) == 2
                        and self._is_int(item[0]) and self._is_int(item[1])
                        and item[0] >= 0 and item[1] >= 1):
                    owner_ranges.append((owner, item[0], item[0] + item[1]))
        for left in range(len(owner_ranges)):
            for right in range(left + 1, len(owner_ranges)):
                owner_a, start_a, end_a = owner_ranges[left]
                owner_b, start_b, end_b = owner_ranges[right]
                if owner_a != owner_b and start_a < end_b and start_b < end_a:
                    self._issue(issues, "subordinate_overlap",
                                "subordinate ranges of %r and %r overlap"
                                % (owner_a, owner_b))

        return {
            "ok": not issues,
            "issues": issues,
            "uid_map": uid_map,
            "gid_map": gid_map,
            "extras": extras,
        }

    def validate_helper_args(self, lines):
        issues = []
        entries = []

        if not lines:
            self._issue(issues, "empty_map",
                        "no mapping lines given to the helper")
        else:
            for line in lines:
                parsed = self._parse_entry(line)
                if parsed is None:
                    self._issue(issues, "bad_entry",
                                "helper line %r is not three non-negative "
                                "integers with a positive length" % (line,))
                    continue
                container_start, host_start, length = parsed
                if (container_start + length > self.id_limit
                        or host_start + length > self.id_limit):
                    self._issue(issues, "overflow",
                                "helper line %r wraps past the id limit"
                                % (parsed,))
                entries.append(parsed)
            if len(entries) > self.max_lines:
                self._issue(issues, "too_many_lines",
                            "%d mapping lines given, limit is %d"
                            % (len(entries), self.max_lines))

        return {"ok": not issues, "issues": issues, "entries": entries}
