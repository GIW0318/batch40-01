import re


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")

LIMIT_KEYS = ("memory.max", "memory.high", "memory.swap.max", "pids.max", "cpu.max")

CPU_PERIOD_MIN = 1000
CPU_PERIOD_MAX = 1000000
CPU_QUOTA_MIN = 1000


class CgroupLimitGuard:
    """Validate and apply cgroup v2 limits across a group tree."""

    def __init__(self, root="/sys/fs/cgroup", max_name_length=64):
        self.root = root
        self.max_name_length = max_name_length
        self._groups = {"/": self._new_node("/")}

    def _new_node(self, path):
        return {
            "path": path,
            "parent": None,
            "memory.max": None,
            "memory.high": None,
            "memory.swap.max": None,
            "pids.max": None,
            "cpu.max": None,
            "owner": None,
            "controllers": [],
            "subtree_control": [],
            "usage": {"memory": 0, "pids": 0},
            "children": [],
        }

    def _validate_name(self, name):
        if not name or name in (".", ".."):
            raise ValueError("unsafe cgroup name: %r" % name)
        if len(name) > self.max_name_length:
            raise ValueError("cgroup name too long: %r" % name)
        if not _SAFE_NAME.match(name):
            raise ValueError("unsafe cgroup name: %r" % name)

    def _split(self, path):
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("group path must be absolute: %r" % (path,))
        parts = [part for part in path.split("/") if part != ""]
        for part in parts:
            self._validate_name(part)
        return parts

    def _normalize(self, path):
        return "/" + "/".join(self._split(path))

    def _lookup(self, path):
        norm = self._normalize(path)
        try:
            return self._groups[norm]
        except KeyError:
            raise KeyError("no such cgroup: %s" % norm)

    def create_group(self, path, parent=None, limits=None, controllers=None):
        parts = self._split(path)
        if not parts:
            raise ValueError("cannot create the root group")
        norm = "/" + "/".join(parts)
        if norm in self._groups:
            raise ValueError("group already exists: %s" % norm)
        expected_parent = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        if parent is None:
            parent = expected_parent
        parent_norm = self._normalize(parent)
        if parent_norm != expected_parent:
            raise ValueError("parent %s does not match path %s" % (parent_norm, norm))
        if parent_norm not in self._groups:
            raise ValueError("parent group does not exist: %s" % parent_norm)
        controllers = list(controllers or [])
        for controller in controllers:
            if not isinstance(controller, str) or not _SAFE_NAME.match(controller):
                raise ValueError("unsafe controller name: %r" % (controller,))
        node = self._new_node(norm)
        node["parent"] = parent_norm
        node["controllers"] = controllers
        self._groups[norm] = node
        self._groups[parent_norm]["children"].append(norm)
        try:
            if limits:
                self.set_limits(norm, limits)
        except Exception:
            del self._groups[norm]
            self._groups[parent_norm]["children"].remove(norm)
            raise
        return dict(node)

    def get(self, path):
        return dict(self._lookup(path))

    def set_usage(self, path, memory=None, pids=None):
        node = self._lookup(path)
        if memory is not None:
            if memory < 0:
                raise ValueError("memory usage cannot be negative")
            node["usage"]["memory"] = memory
        if pids is not None:
            if pids < 0:
                raise ValueError("pids usage cannot be negative")
            node["usage"]["pids"] = pids
        return dict(node)

    def parse_limit(self, key, value):
        if key in ("memory.max", "memory.high", "memory.swap.max", "pids.max"):
            return self._parse_value_or_max(key, value)
        if key == "cpu.max":
            return self._parse_cpu_max(value)
        raise ValueError("unknown limit key: %r" % (key,))

    def _parse_value_or_max(self, key, value):
        if isinstance(value, str):
            text = value.strip()
            if text == "max":
                return None
            try:
                number = int(text, 10)
            except ValueError:
                raise ValueError("invalid value for %s: %r" % (key, value))
        elif isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("invalid value for %s: %r" % (key, value))
        else:
            number = value
        if number < 0:
            raise ValueError("%s cannot be negative: %r" % (key, value))
        return number

    def _parse_cpu_max(self, value):
        if isinstance(value, (tuple, list)) and len(value) == 2:
            quota_raw, period_raw = value
        else:
            parts = str(value).split()
            if len(parts) != 2:
                raise ValueError("cpu.max expects '<quota> <period>': %r" % (value,))
            quota_raw, period_raw = parts
        if isinstance(period_raw, str):
            try:
                period = int(period_raw, 10)
            except ValueError:
                raise ValueError("invalid cpu.max period: %r" % (period_raw,))
        else:
            period = period_raw
        if not isinstance(period, int) or isinstance(period, bool):
            raise ValueError("invalid cpu.max period: %r" % (period_raw,))
        if not CPU_PERIOD_MIN <= period <= CPU_PERIOD_MAX:
            raise ValueError(
                "cpu.max period %d out of range [%d, %d]"
                % (period, CPU_PERIOD_MIN, CPU_PERIOD_MAX)
            )
        if isinstance(quota_raw, str) and quota_raw.strip() == "max":
            quota = None
        else:
            if isinstance(quota_raw, str):
                try:
                    quota = int(quota_raw, 10)
                except ValueError:
                    raise ValueError("invalid cpu.max quota: %r" % (quota_raw,))
            else:
                quota = quota_raw
            if not isinstance(quota, int) or isinstance(quota, bool):
                raise ValueError("invalid cpu.max quota: %r" % (quota_raw,))
            if quota < CPU_QUOTA_MIN:
                raise ValueError("cpu.max quota %d below minimum %d" % (quota, CPU_QUOTA_MIN))
            if quota > period:
                raise ValueError(
                    "cpu.max quota %d exceeds period %d" % (quota, period)
                )
        return (quota, period)

    def set_limits(self, path, limits):
        node = self._lookup(path)
        parsed = {}
        for key, value in limits.items():
            parsed[key] = self.parse_limit(key, value)

        if "memory.max" in parsed:
            new_max = parsed["memory.max"]
            usage = node["usage"]["memory"]
            if new_max is not None and new_max < usage:
                raise ValueError(
                    "memory.max %d below current usage %d" % (new_max, usage)
                )
            parent_path = node["parent"]
            if parent_path is not None:
                parent_max = self._groups[parent_path]["memory.max"]
                if parent_max is not None and (new_max is None or new_max > parent_max):
                    raise ValueError(
                        "memory.max %s exceeds parent limit %d"
                        % ("max" if new_max is None else new_max, parent_max)
                    )

        if "memory.high" in parsed or "memory.max" in parsed:
            effective_high = parsed.get("memory.high", node["memory.high"])
            effective_max = parsed.get("memory.max", node["memory.max"])
            if (
                effective_high is not None
                and effective_max is not None
                and effective_high > effective_max
            ):
                raise ValueError(
                    "memory.high %d above memory.max %d" % (effective_high, effective_max)
                )

        node.update(parsed)
        return dict(node)

    def set_many(self, changes):
        snapshot = {
            path: {key: node[key] for key in LIMIT_KEYS}
            for path, node in self._groups.items()
        }
        results = []
        try:
            for change in changes:
                results.append(self.set_limits(change["path"], change["limits"]))
        except ValueError as exc:
            for path, saved in snapshot.items():
                self._groups[path].update(saved)
            raise ValueError("batch change rolled back: %s" % exc)
        return results

    def delegate(self, path, owner, controllers):
        node = self._lookup(path)
        if not isinstance(owner, str) or not owner:
            raise ValueError("delegation requires a non-empty owner")
        requested = list(controllers)
        available = set(node["controllers"])
        missing = [c for c in requested if c not in available]
        if missing:
            raise ValueError(
                "controllers not available for delegation: %s" % ", ".join(missing)
            )
        node["owner"] = owner
        node["subtree_control"] = requested
        return {
            "delegated": True,
            "owner": owner,
            "subtree_control": list(requested),
        }

    def stats(self, path):
        node = self._lookup(path)
        memory = node["usage"]["memory"]
        pids = node["usage"]["pids"]
        for child_path in node["children"]:
            child_stats = self.stats(child_path)
            memory += child_stats["memory"]
            pids += child_stats["pids"]
        return {"memory": memory, "pids": pids}

    def can_fork(self, path, count=1):
        node = self._lookup(path)
        limit = node["pids.max"]
        if limit is None:
            return True
        return node["usage"]["pids"] + count <= limit
