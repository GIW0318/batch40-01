"""Parse and intersect seccomp profiles across architectures."""

import re

ALLOW = "SCMP_ACT_ALLOW"
LOG = "SCMP_ACT_LOG"
NOTIFY = "SCMP_ACT_NOTIFY"
ERRNO = "SCMP_ACT_ERRNO"
TRACE = "SCMP_ACT_TRACE"
TRAP = "SCMP_ACT_TRAP"
KILL = "SCMP_ACT_KILL"
KILL_PROCESS = "SCMP_ACT_KILL_PROCESS"

_ACTION_STRICTNESS = {
    ALLOW: 0,
    LOG: 1,
    NOTIFY: 2,
    ERRNO: 3,
    TRACE: 4,
    TRAP: 5,
    KILL: 6,
    KILL_PROCESS: 7,
}

_ACTION_KEYWORDS = {
    "allow": ALLOW,
    "log": LOG,
    "notify": NOTIFY,
    "errno": ERRNO,
    "trace": TRACE,
    "trap": TRAP,
    "kill": KILL,
    "kill_process": KILL_PROCESS,
}

_CMP_OPS = {
    "==": "SCMP_CMP_EQ",
    "!=": "SCMP_CMP_NE",
    ">=": "SCMP_CMP_GE",
    ">": "SCMP_CMP_GT",
    "<=": "SCMP_CMP_LE",
    "<": "SCMP_CMP_LT",
}

_ARCH_ALIASES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "x86": "x86",
    "i386": "x86",
    "i486": "x86",
    "i586": "x86",
    "i686": "x86",
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "arm": "arm",
    "armv7": "arm",
}

_SYSCALL_NUMBERS = {
    "x86_64": {
        "read": 0, "write": 1, "open": 2, "close": 3, "stat": 4,
        "fstat": 5, "lseek": 8, "mmap": 9, "mprotect": 10, "munmap": 11,
        "brk": 12, "ioctl": 16, "access": 21, "ptrace": 101, "openat": 257,
    },
    "x86": {
        "exit": 1, "read": 3, "write": 4, "open": 5, "close": 6,
        "lseek": 19, "ptrace": 26, "access": 33, "brk": 45, "ioctl": 54,
        "mmap": 90, "munmap": 91, "mprotect": 125, "stat": 106,
        "fstat": 108, "openat": 295,
    },
    "aarch64": {
        "openat": 56, "close": 57, "read": 63, "write": 64,
        "fstat": 80, "lseek": 62, "mmap": 222, "mprotect": 226,
        "munmap": 215, "brk": 214, "ioctl": 29, "ptrace": 117,
    },
    "arm": {
        "read": 3, "write": 4, "open": 5, "close": 6, "lseek": 19,
        "ptrace": 26, "access": 33, "brk": 45, "ioctl": 54, "mmap": 90,
        "munmap": 91, "mprotect": 125, "stat": 106, "fstat": 108,
        "openat": 322,
    },
}

_DEFAULT_ARCH = "x86_64"
_MAX_INCLUDE_DEPTH = 16

_INCLUDE_RE = re.compile(r'^#include\s+"([^"]+)"\s*$')
_CONDITION_RE = re.compile(
    r"^arg(\d+)\s*(==|!=|>=|<=|>|<)\s*(0[xX][0-9a-fA-F]+|\d+)$"
)


def _normalize_arch(name):
    key = name.strip()
    if key.upper().startswith("SCMP_ARCH_"):
        key = key[len("SCMP_ARCH_"):]
    key = key.lower()
    return _ARCH_ALIASES.get(key, key)


def _stricter(action_a, action_b):
    if _ACTION_STRICTNESS[action_a] >= _ACTION_STRICTNESS[action_b]:
        return action_a
    return action_b


class SeccompProfileMerge:
    """Parse and intersect seccomp profiles across architectures."""

    def __init__(self, known_syscalls=None):
        if known_syscalls is None:
            known = set()
            for table in _SYSCALL_NUMBERS.values():
                known.update(table)
            self._known = known
        elif isinstance(known_syscalls, dict):
            self._known = set(known_syscalls)
        else:
            self._known = set(known_syscalls)

    def parse(self, text, includes=None):
        profile = {
            "default": None,
            "arches": {},
            "conflicts": [],
            "notify": False,
        }
        state = {"arch": None}
        self._parse_lines(text.splitlines(), includes or {}, profile, state, 0)
        return profile

    def merge(self, base, overlay, allow_notify=False):
        base_default = base.get("default") or ALLOW
        overlay_default = overlay.get("default") or ALLOW
        notify_involved = (
            base.get("notify")
            or overlay.get("notify")
            or base_default == NOTIFY
            or overlay_default == NOTIFY
        )
        if notify_involved and not allow_notify:
            raise ValueError(
                "SCMP_ACT_NOTIFY requires allow_notify=True to merge"
            )
        merged = {
            "default": _stricter(base_default, overlay_default),
            "arches": {},
            "conflicts": list(base.get("conflicts", []))
            + list(overlay.get("conflicts", [])),
            "notify": bool(notify_involved),
        }
        arch_names = list(base["arches"])
        arch_names += [a for a in overlay["arches"] if a not in base["arches"]]
        for arch in arch_names:
            base_syscalls = base["arches"].get(arch, {}).get("syscalls", {})
            overlay_syscalls = overlay["arches"].get(arch, {}).get("syscalls", {})
            merged_syscalls = {}
            names = list(base_syscalls)
            names += [n for n in overlay_syscalls if n not in base_syscalls]
            for name in names:
                base_entry = base_syscalls.get(name)
                overlay_entry = overlay_syscalls.get(name)
                if base_entry is not None and overlay_entry is not None:
                    entry = self._combine_entries(
                        base_entry, overlay_entry, merged["conflicts"],
                        arch, name,
                    )
                elif base_entry is not None:
                    entry = self._combine_with_default(base_entry, overlay_default)
                else:
                    entry = self._combine_with_default(overlay_entry, base_default)
                merged_syscalls[name] = entry
                if entry["action"] == NOTIFY:
                    merged["notify"] = True
            merged["arches"][arch] = {"syscalls": merged_syscalls}
        return merged

    def decide(self, profile, arch, syscall, args=None):
        arch = _normalize_arch(arch)
        default = profile.get("default") or ALLOW
        section = profile.get("arches", {}).get(arch)
        if section is None:
            return default
        name = self._resolve_syscall(syscall, arch, strict=False)
        if name is None:
            return default
        entry = section["syscalls"].get(name)
        if entry is None:
            return default
        conditions = entry.get("args") or []
        if conditions:
            values = args or {}
            for condition in conditions:
                value = values.get("arg%d" % condition["index"])
                if value is None or not self._eval_condition(condition, value):
                    return default
        return entry["action"]

    def _parse_lines(self, lines, includes, profile, state, depth):
        if depth > _MAX_INCLUDE_DEPTH:
            raise ValueError("include nesting too deep")
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            include_match = _INCLUDE_RE.match(line)
            if include_match:
                name = include_match.group(1)
                if name not in includes:
                    raise ValueError("unknown include: %s" % name)
                self._parse_lines(
                    includes[name].splitlines(), includes, profile, state,
                    depth + 1,
                )
                continue
            if line.startswith("#"):
                continue
            hash_pos = line.find("#")
            if hash_pos != -1:
                line = line[:hash_pos].strip()
            if not line:
                continue
            self._parse_statement(line, profile, state)

    def _parse_statement(self, line, profile, state):
        if line.startswith("default"):
            rest = line[len("default"):].strip()
            if rest.startswith("="):
                rest = rest[1:].strip()
            action = self._action_token(rest)
            profile["default"] = action
            if action == NOTIFY:
                profile["notify"] = True
            return
        if line.startswith("arch"):
            rest = line[len("arch"):].strip()
            if rest.startswith("="):
                rest = rest[1:].strip()
            if not rest:
                raise ValueError("arch directive requires a name")
            arch = _normalize_arch(rest)
            state["arch"] = arch
            profile["arches"].setdefault(arch, {"syscalls": {}})
            return
        head, _, condition_text = line.partition(" if ")
        tokens = head.split()
        action = self._action_token(tokens[0])
        rest = tokens[1:]
        errno_name = None
        if action == ERRNO and len(rest) >= 2:
            errno_name = rest[0]
            rest = rest[1:]
        if len(rest) != 1:
            raise ValueError("malformed rule: %r" % line)
        arch = state["arch"] or _DEFAULT_ARCH
        profile["arches"].setdefault(arch, {"syscalls": {}})
        name = self._resolve_syscall(rest[0], arch, strict=True)
        conditions = []
        if condition_text.strip():
            conditions = self._parse_conditions(condition_text.strip())
        entry = {"action": action, "args": conditions}
        if errno_name is not None:
            entry["errno"] = errno_name
        syscalls = profile["arches"][arch]["syscalls"]
        if name in syscalls:
            profile["conflicts"].append({
                "arch": arch,
                "syscall": name,
                "detail": "duplicate rule; later rule wins",
            })
        syscalls[name] = entry
        if action == NOTIFY:
            profile["notify"] = True

    def _action_token(self, token):
        if token in _ACTION_STRICTNESS:
            return token
        action = _ACTION_KEYWORDS.get(token.lower())
        if action is None:
            raise ValueError("unknown action: %s" % token)
        return action

    def _resolve_syscall(self, token, arch, strict):
        text = str(token)
        if text.isdigit() or text.lower().startswith("0x"):
            number = int(text, 0)
            table = _SYSCALL_NUMBERS.get(arch, {})
            for name, known_number in table.items():
                if known_number == number:
                    return name
            if strict:
                raise ValueError(
                    "unknown syscall number %s for arch %s" % (token, arch)
                )
            return None
        if text not in self._known:
            if strict:
                raise ValueError("unknown syscall: %s" % text)
            return None
        return text

    def _parse_conditions(self, text):
        conditions = []
        for part in re.split(r"\s+and\s+", text):
            match = _CONDITION_RE.match(part.strip())
            if not match:
                raise ValueError("malformed argument filter: %r" % part)
            index, op, value = match.groups()
            conditions.append({
                "index": int(index),
                "op": _CMP_OPS[op],
                "value": int(value, 0),
            })
        return conditions

    def _combine_entries(self, base_entry, overlay_entry, conflicts, arch, name):
        base_action = base_entry["action"]
        overlay_action = overlay_entry["action"]
        base_rank = _ACTION_STRICTNESS[base_action]
        overlay_rank = _ACTION_STRICTNESS[overlay_action]
        errno_name = None
        if base_rank > overlay_rank:
            action = base_action
            errno_name = base_entry.get("errno")
        elif overlay_rank > base_rank:
            action = overlay_action
            errno_name = overlay_entry.get("errno")
        else:
            action = base_action
            base_errno = base_entry.get("errno")
            overlay_errno = overlay_entry.get("errno")
            if base_action == ERRNO and base_errno != overlay_errno:
                conflicts.append({
                    "arch": arch,
                    "syscall": name,
                    "detail": "conflicting errno codes %r vs %r; keeping base"
                              % (base_errno, overlay_errno),
                })
            errno_name = base_errno if base_errno is not None else overlay_errno
        args = list(base_entry.get("args") or [])
        for condition in overlay_entry.get("args") or []:
            if condition not in args:
                args.append(condition)
        entry = {"action": action, "args": args}
        if errno_name is not None:
            entry["errno"] = errno_name
        return entry

    def _combine_with_default(self, entry, other_default):
        action = _stricter(entry["action"], other_default)
        merged = {"action": action, "args": list(entry.get("args") or [])}
        if action == entry["action"] and entry.get("errno") is not None:
            merged["errno"] = entry["errno"]
        return merged

    @staticmethod
    def _eval_condition(condition, value):
        op = condition["op"]
        target = condition["value"]
        if op == "SCMP_CMP_EQ":
            return value == target
        if op == "SCMP_CMP_NE":
            return value != target
        if op == "SCMP_CMP_GE":
            return value >= target
        if op == "SCMP_CMP_GT":
            return value > target
        if op == "SCMP_CMP_LE":
            return value <= target
        if op == "SCMP_CMP_LT":
            return value < target
        raise ValueError("unknown comparison op: %s" % op)
