"""Asset hot reload dependency graph with debounce and lease safety."""

import heapq


class HotReloadGraph:
    def __init__(self, debounce_ms=200, large_asset_mb=50.0):
        self.debounce_ms = debounce_ms
        self.large_asset_mb = large_asset_mb
        self.assets = {}
        self.last_event_ms = {}
        self.versions = {}
        self.leases = {}
        self.variant_cache = {}
        self.mtimes = {}
        self._lease_refs = {}
        self._retired_versions = {}
        self._next_lease = 1

    @staticmethod
    def _validate_path(path):
        if not isinstance(path, str) or not path.strip():
            raise ValueError("asset path must be a non-empty string")

    def _require_asset(self, path):
        self._validate_path(path)
        if path not in self.assets:
            raise ValueError(f"unknown asset: {path}")

    def add_asset(self, path, deps=(), size_mb=0.0, kind="model", mtime=0):
        self._validate_path(path)

        clean_deps = []
        for dep in deps:
            self._validate_path(dep)
            if dep not in clean_deps:
                clean_deps.append(dep)

        entry = self.assets.get(path)
        if entry is None:
            entry = {"path": path}
            self.assets[path] = entry
            if path in self._retired_versions:
                self.versions[path] = self._retired_versions.pop(path)

        entry.update(
            {
                "deps": clean_deps,
                "size_mb": float(size_mb),
                "kind": kind,
                "mtime": mtime,
            }
        )
        self.mtimes[path] = mtime
        return entry

    def remove_asset(self, path):
        self._validate_path(path)
        if path not in self.assets:
            return False

        affected = [path]
        affected.extend(self.dependents(path))
        for affected_path in affected:
            self.variant_cache.pop(affected_path, None)

        del self.assets[path]
        self.mtimes.pop(path, None)

        for entry in self.assets.values():
            if path in entry["deps"]:
                entry["deps"] = [dep for dep in entry["deps"] if dep != path]

        if self._lease_refs.get(path, 0):
            self._retired_versions[path] = self.versions.get(path)
        else:
            self.versions.pop(path, None)
            self._retired_versions.pop(path, None)

        return True

    def deps(self, path):
        self._validate_path(path)
        entry = self.assets.get(path)
        return list(entry["deps"]) if entry else []

    def dependents(self, path):
        self._validate_path(path)
        if path not in self.assets:
            return []

        reverse = {}
        for asset_path, entry in self.assets.items():
            for dep in entry["deps"]:
                reverse.setdefault(dep, set()).add(asset_path)

        seen = {path}
        stack = [path]
        while stack:
            current = stack.pop()
            for dependent in reverse.get(current, ()):
                if dependent not in seen:
                    seen.add(dependent)
                    stack.append(dependent)

        seen.discard(path)
        return sorted(seen)

    def reload_order(self, changed):
        roots = []
        for path in changed:
            self._require_asset(path)
            if path not in roots:
                roots.append(path)
        if not roots:
            return []

        affected = set(roots)
        for path in roots:
            affected.update(self.dependents(path))
        return self._topological_order(affected)

    def _topological_order(self, nodes):
        dependency_edges = {
            node: [dep for dep in self.assets[node]["deps"] if dep in nodes]
            for node in nodes
        }

        visited = set()
        finish_order = []
        for start in sorted(nodes):
            if start in visited:
                continue
            visited.add(start)
            stack = [(start, iter(dependency_edges[start]))]
            while stack:
                node, children = stack[-1]
                try:
                    child = next(children)
                except StopIteration:
                    finish_order.append(node)
                    stack.pop()
                    continue
                if child not in visited:
                    visited.add(child)
                    stack.append((child, iter(dependency_edges[child])))

        reverse_edges = {node: [] for node in nodes}
        for node, dependencies in dependency_edges.items():
            for dependency in dependencies:
                reverse_edges[dependency].append(node)

        component_of = {}
        components = []
        for start in reversed(finish_order):
            if start in component_of:
                continue
            component_index = len(components)
            component = []
            stack = [start]
            component_of[start] = component_index
            while stack:
                node = stack.pop()
                component.append(node)
                for parent in reverse_edges[node]:
                    if parent not in component_of:
                        component_of[parent] = component_index
                        stack.append(parent)
            component.sort()
            components.append(component)

        component_deps = [set() for _ in components]
        component_dependents = [set() for _ in components]
        for node, dependencies in dependency_edges.items():
            node_component = component_of[node]
            for dependency in dependencies:
                dependency_component = component_of[dependency]
                if node_component == dependency_component:
                    continue
                component_deps[node_component].add(dependency_component)
                component_dependents[dependency_component].add(node_component)

        indegree = [len(dependencies) for dependencies in component_deps]
        ready = []
        for index, degree in enumerate(indegree):
            if degree == 0:
                heapq.heappush(ready, (components[index][0], index))

        ordered = []
        while ready:
            _, component_index = heapq.heappop(ready)
            ordered.extend(components[component_index])
            for dependent in component_dependents[component_index]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(
                        ready, (components[dependent][0], dependent)
                    )
        return ordered

    def watch(self, events, now_ms):
        grouped = {}
        for sequence, event in enumerate(events):
            path = event.get("path")
            if not isinstance(path, str) or path not in self.assets:
                continue
            at_ms = event.get("at_ms", now_ms)
            grouped.setdefault(path, []).append((at_ms, sequence))

        batches = []
        for path, path_events in grouped.items():
            path_events.sort()
            first_at = path_events[0][0]
            last_at = first_at
            count = 0
            for at_ms, _ in path_events:
                if count and at_ms - last_at >= self.debounce_ms:
                    batches.append((first_at, path, last_at, count))
                    first_at = at_ms
                    count = 0
                last_at = at_ms
                count += 1
            batches.append((first_at, path, last_at, count))

        batches.sort(key=lambda batch: (batch[0], batch[1]))
        jobs = []
        for _, path, last_at, count in batches:
            self.last_event_ms[path] = last_at
            jobs.append(
                {
                    "path": path,
                    "at_ms": now_ms,
                    "last_event_ms": last_at,
                    "event_count": count,
                }
            )
        return jobs

    def swap(self, path, version, valid=True):
        self._require_asset(path)
        if not valid:
            return False

        self.versions[path] = version
        self._retired_versions.pop(path, None)
        self.invalidate_variants(path)
        return True

    def version(self, path):
        self._validate_path(path)
        return self.versions.get(path)

    def read(self, path):
        self._validate_path(path)
        if path in self.assets:
            return self.versions.get(path)
        if self._lease_refs.get(path, 0):
            return self._retired_versions.get(path)
        return None

    def lease(self, path):
        self._require_asset(path)
        handle = self._next_lease
        self._next_lease += 1
        self.leases[handle] = {
            "path": path,
            "version": self.versions.get(path),
        }
        self._lease_refs[path] = self._lease_refs.get(path, 0) + 1
        return handle

    def release(self, handle):
        record = self.leases.pop(handle, None)
        if record is None:
            return False

        path = record["path"]
        remaining = self._lease_refs.get(path, 0) - 1
        if remaining > 0:
            self._lease_refs[path] = remaining
        else:
            self._lease_refs.pop(path, None)
            if path not in self.assets:
                self.versions.pop(path, None)
                self._retired_versions.pop(path, None)
        return True

    def variants(self, path):
        self._validate_path(path)
        return list(self.variant_cache.get(path, ()))

    def cache_variant(self, path, variant):
        self._require_asset(path)
        variants = self.variant_cache.setdefault(path, [])
        if variant not in variants:
            variants.append(variant)

    def invalidate_variants(self, path):
        self._validate_path(path)
        self.variant_cache.pop(path, None)
        for dependent in self.dependents(path):
            self.variant_cache.pop(dependent, None)

    def reload(self, path, size_mb=None):
        self._require_asset(path)
        if size_mb is None:
            size_mb = self.assets[path]["size_mb"]
        size_mb = float(size_mb)
        deferred = size_mb >= self.large_asset_mb
        return {
            "path": path,
            "size_mb": size_mb,
            "deferred": deferred,
        }

    def poll(self, snapshot):
        changed = []
        for path, entry in self.assets.items():
            if path not in snapshot:
                continue
            mtime = snapshot[path]
            if mtime != entry["mtime"]:
                entry["mtime"] = mtime
                self.mtimes[path] = mtime
                changed.append(path)
        return sorted(changed)
