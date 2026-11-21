from collections import deque
import os


_FS_RIGHTS_ABI1 = frozenset(
    {
        "execute",
        "write_file",
        "read_file",
        "read_dir",
        "remove_dir",
        "remove_file",
        "make_char",
        "make_dir",
        "make_reg",
        "make_sock",
        "make_fifo",
        "make_block",
        "make_sym",
    }
)

_ABI_RIGHTS = {
    1: _FS_RIGHTS_ABI1,
    2: _FS_RIGHTS_ABI1 | {"refer"},
    3: _FS_RIGHTS_ABI1 | {"refer", "truncate"},
    4: _FS_RIGHTS_ABI1 | {"refer", "truncate", "bind_tcp", "connect_tcp"},
    5: _FS_RIGHTS_ABI1
    | {"refer", "truncate", "bind_tcp", "connect_tcp", "ioctl_dev"},
}

_MAX_SYMLINK_EXPANSIONS = 40


class LandlockRulesetFence:
    """Userspace model of Landlock ruleset construction and enforcement."""

    def __init__(self, abi=3, max_path=4096, symlinks=None, mounts=None):
        if isinstance(abi, bool) or not isinstance(abi, int) or abi not in _ABI_RIGHTS:
            raise ValueError(f"unsupported Landlock ABI: {abi!r}")
        if (
            isinstance(max_path, bool)
            or not isinstance(max_path, int)
            or max_path <= 0
        ):
            raise ValueError("max_path must be a positive integer")

        self.abi = abi
        self.max_path = max_path
        self.symlinks = self._normalize_symlinks(symlinks or {})
        self.mounts = self._normalize_mounts(mounts or {})

        self._next_ruleset_id = 1
        self._rulesets = {}
        self._restrictions = {}

    def rights_for_abi(self, abi=None):
        selected = self.abi if abi is None else abi
        if (
            isinstance(selected, bool)
            or not isinstance(selected, int)
            or selected not in _ABI_RIGHTS
        ):
            raise ValueError(f"unsupported Landlock ABI: {selected!r}")
        return set(_ABI_RIGHTS[selected])

    def create_ruleset(self, rights, abi=None):
        selected = self.abi if abi is None else abi
        available = self.rights_for_abi(selected)
        requested = self._coerce_rights(rights)

        unsupported = requested - available
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ValueError(
                f"rights not supported by Landlock ABI {selected}: {names}"
            )

        ruleset_id = self._next_ruleset_id
        self._next_ruleset_id += 1
        self._rulesets[ruleset_id] = {
            "id": ruleset_id,
            "abi": selected,
            "rights": frozenset(requested),
            "rules": [],
            "enforced": False,
        }
        return {
            "id": ruleset_id,
            "abi": selected,
            "rights": set(requested),
        }

    def add_rule(self, ruleset_id, path, access, kind="dir", allow=True):
        ruleset = self._get_ruleset(ruleset_id)
        if ruleset["enforced"]:
            raise ValueError("rules cannot be added after restrict")
        if kind not in {"dir", "file"}:
            raise ValueError("kind must be 'dir' or 'file'")
        if not isinstance(allow, bool):
            raise ValueError("allow must be a boolean")

        requested = self._coerce_rights(access)
        unhandled = requested - ruleset["rights"]
        if unhandled:
            names = ", ".join(sorted(unhandled))
            raise ValueError(f"access rights not handled by ruleset: {names}")

        self._validate_path(path)
        if not path.startswith("/"):
            raise ValueError("rule paths must be absolute")
        resolved = self.resolve_path(path, cwd="/")

        rule = {
            "path": resolved,
            "original_path": path,
            "access": frozenset(requested),
            "kind": kind,
            "allow": allow,
        }
        ruleset["rules"].append(rule)
        return {
            "path": resolved,
            "kind": kind,
            "access": set(requested),
            "allow": allow,
        }

    def restrict(self, ruleset_id, pid):
        ruleset = self._get_ruleset(ruleset_id)
        if not ruleset["rules"]:
            raise ValueError("cannot enforce an empty ruleset")

        pid = self._validate_pid(pid)
        ruleset["enforced"] = True
        stack = self._restrictions.setdefault(pid, [])
        if ruleset_id not in stack:
            stack.append(ruleset_id)
        return self._restriction_fingerprint(pid)

    def spawn(self, parent_pid, child_pid):
        parent_pid = self._validate_pid(parent_pid)
        child_pid = self._validate_pid(child_pid)

        inherited = self._restrictions.get(parent_pid)
        if inherited is None:
            self._restrictions.pop(child_pid, None)
        else:
            self._restrictions[child_pid] = list(inherited)

        fingerprint = self._restriction_fingerprint(child_pid)
        expected = self._restriction_fingerprint(parent_pid)
        if fingerprint != expected:
            self._restrictions.pop(child_pid, None)
            raise RuntimeError("child did not inherit the parent restriction")
        return fingerprint

    def check(self, pid, path, access):
        pid = self._validate_pid(pid)
        requested = self._coerce_rights(access)
        self._validate_path(path)
        if not path.startswith("/"):
            raise ValueError("query paths must be absolute")
        resolved = self.resolve_path(path, cwd="/")

        stack = self._restrictions.get(pid)
        if not stack:
            return True

        handled = self._handled_rights(stack)
        unhandled = requested - handled
        if unhandled:
            names = ", ".join(sorted(unhandled))
            raise ValueError(f"access rights are not handled: {names}")

        for right in requested:
            for ruleset_id in stack:
                ruleset = self._rulesets[ruleset_id]
                if right not in ruleset["rights"]:
                    continue
                allowed, _, _ = self._decide(ruleset, resolved, right)
                if not allowed:
                    return False
        return True

    def explain(self, pid, path, access):
        pid = self._validate_pid(pid)
        requested = self._coerce_rights(access)
        self._validate_path(path)
        if not path.startswith("/"):
            raise ValueError("query paths must be absolute")
        resolved = self.resolve_path(path, cwd="/")

        stack = self._restrictions.get(pid)
        if not stack:
            decisions = {
                right: {
                    "allowed": True,
                    "layers": [],
                    "reason": "process is not restricted",
                }
                for right in sorted(requested)
            }
            return {
                "allowed": True,
                "pid": pid,
                "path": resolved,
                "input_path": path,
                "restricted": False,
                "ruleset_ids": [],
                "decisions": decisions,
                "reason": "process is not restricted",
            }

        handled = self._handled_rights(stack)
        unhandled = requested - handled
        if unhandled:
            names = ", ".join(sorted(unhandled))
            raise ValueError(f"access rights are not handled: {names}")

        decisions = {}
        overall = True
        for right in sorted(requested):
            layers = []
            right_allowed = True
            for ruleset_id in stack:
                ruleset = self._rulesets[ruleset_id]
                if right not in ruleset["rights"]:
                    continue
                layer_allowed, rule, reason = self._decide(
                    ruleset, resolved, right
                )
                layers.append(
                    {
                        "ruleset_id": ruleset_id,
                        "allowed": layer_allowed,
                        "reason": reason,
                        "matched_rule": self._public_rule(rule),
                    }
                )
                right_allowed = right_allowed and layer_allowed
            decisions[right] = {
                "allowed": right_allowed,
                "layers": layers,
                "reason": "all restriction layers allow"
                if right_allowed
                else "at least one restriction layer denies",
            }
            overall = overall and right_allowed

        return {
            "allowed": overall,
            "pid": pid,
            "path": resolved,
            "input_path": path,
            "restricted": True,
            "ruleset_ids": list(stack),
            "decisions": decisions,
            "reason": "access allowed" if overall else "access denied",
        }

    def resolve_path(self, path, cwd="/"):
        self._validate_path(path)
        if not isinstance(cwd, str) or not cwd.startswith("/"):
            raise ValueError("cwd must be an absolute path")
        if "\x00" in cwd:
            raise ValueError("cwd contains a NUL byte")

        if path.startswith("/"):
            pending = deque(self._components(path))
        else:
            pending = deque(self._components(cwd) + self._components(path))

        resolved = []
        expansions = 0
        while pending:
            component = pending.popleft()
            if component == ".":
                continue
            if component == "..":
                if resolved:
                    resolved.pop()
                continue

            candidate = "/" + "/".join(resolved + [component])
            if candidate in self.mounts:
                expansions += 1
                self._check_expansion_limit(expansions)
                pending = deque(
                    self._components(self.mounts[candidate]) + list(pending)
                )
                resolved = []
                continue

            if candidate in self.symlinks:
                expansions += 1
                self._check_expansion_limit(expansions)
                target = self.symlinks[candidate]
                if target.startswith("/"):
                    resolved = []
                pending = deque(self._components(target) + list(pending))
                continue

            resolved.append(component)

        result = "/" if not resolved else "/" + "/".join(resolved)
        if self._path_too_long(result):
            raise ValueError("resolved path exceeds max_path")
        return result

    def _decide(self, ruleset, path, right):
        matches = [
            rule
            for rule in ruleset["rules"]
            if right in rule["access"] and self._rule_matches(path, rule)
        ]
        if not matches:
            return False, None, "no matching rule; access is denied by default"

        winner = max(matches, key=self._specificity)
        if winner["allow"]:
            return True, winner, "the most specific matching rule allows access"
        return False, winner, "the most specific matching rule denies access"

    def _rule_matches(self, path, rule):
        rule_path = rule["path"]
        if rule["kind"] == "file":
            return path == rule_path
        if path == rule_path:
            return True
        prefix = "/" if rule_path == "/" else rule_path + "/"
        return path.startswith(prefix)

    def _specificity(self, rule):
        path = rule["path"]
        depth = 0 if path == "/" else path.count("/")
        kind_rank = 1 if rule["kind"] == "file" else 0
        deny_rank = 0 if rule["allow"] else 1
        return depth, kind_rank, deny_rank

    def _public_rule(self, rule):
        if rule is None:
            return None
        return {
            "path": rule["path"],
            "original_path": rule["original_path"],
            "access": set(rule["access"]),
            "kind": rule["kind"],
            "allow": rule["allow"],
        }

    def _handled_rights(self, stack):
        rights = set()
        for ruleset_id in stack:
            rights.update(self._rulesets[ruleset_id]["rights"])
        return rights

    def _restriction_fingerprint(self, pid):
        return tuple(self._restrictions.get(pid, ()))

    def _get_ruleset(self, ruleset_id):
        try:
            return self._rulesets[ruleset_id]
        except (KeyError, TypeError):
            raise ValueError(f"unknown ruleset id: {ruleset_id!r}") from None

    def _coerce_rights(self, rights):
        if isinstance(rights, str):
            raise ValueError("rights must be a collection, not a string")
        try:
            requested = set(rights)
        except TypeError:
            raise ValueError("rights must be an iterable of names") from None
        if not requested:
            raise ValueError("at least one access right is required")
        if any(not isinstance(right, str) or not right for right in requested):
            raise ValueError("access rights must be non-empty strings")
        return requested

    def _validate_pid(self, pid):
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
            raise ValueError(f"invalid pid: {pid!r}")
        return pid

    def _validate_path(self, path):
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        if "\x00" in path:
            raise ValueError("path contains a NUL byte")
        if self._path_too_long(path):
            raise ValueError("path exceeds max_path")

    def _path_too_long(self, path):
        if len(path) > self.max_path:
            return True
        try:
            return len(os.fsencode(path)) > self.max_path
        except UnicodeEncodeError:
            return True

    def _check_expansion_limit(self, expansions):
        if expansions > _MAX_SYMLINK_EXPANSIONS:
            raise ValueError("too many symlink or mount expansions")

    def _components(self, path):
        return [part for part in path.split("/") if part]

    def _normalize_symlinks(self, symlinks):
        if not isinstance(symlinks, dict):
            raise ValueError("symlinks must be a mapping")
        normalized = {}
        for link, target in symlinks.items():
            if not isinstance(link, str) or not link.startswith("/"):
                raise ValueError("symlink paths must be absolute")
            if not isinstance(target, str) or not target:
                raise ValueError("symlink targets must be non-empty strings")
            if "\x00" in link or "\x00" in target:
                raise ValueError("symlink paths must not contain NUL bytes")
            key = self._lexical_normalize(link)
            value = (
                self._lexical_normalize(target)
                if target.startswith("/")
                else self._lexical_normalize(target, require_absolute=False)
            )
            normalized[key] = value
        return normalized

    def _normalize_mounts(self, mounts):
        if not isinstance(mounts, dict):
            raise ValueError("mounts must be a mapping")
        normalized = {}
        for alias, target in mounts.items():
            if not isinstance(alias, str) or not alias.startswith("/"):
                raise ValueError("mount aliases must be absolute")
            if not isinstance(target, str) or not target.startswith("/"):
                raise ValueError("mount targets must be absolute")
            if "\x00" in alias or "\x00" in target:
                raise ValueError("mount paths must not contain NUL bytes")
            normalized[self._lexical_normalize(alias)] = self._lexical_normalize(
                target
            )
        return normalized

    def _lexical_normalize(self, path, require_absolute=True):
        if require_absolute and not path.startswith("/"):
            raise ValueError("path must be absolute")
        parts = []
        for component in self._components(path):
            if component == ".":
                continue
            if component == "..":
                if parts and parts[-1] != "..":
                    parts.pop()
                elif not path.startswith("/"):
                    parts.append(component)
                continue
            parts.append(component)
        prefix = "/" if path.startswith("/") else ""
        if not parts:
            return "/" if prefix else "."
        return prefix + "/".join(parts)
