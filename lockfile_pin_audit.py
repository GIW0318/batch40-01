"""Audit a lock file and rebuild the dependency graph only from it."""

import json
import re


_CONFLICT_MARKER = re.compile(
    r"^[ \t]*(?:<{7}|={7}|>{7})(?:[ \t].*)?$", re.MULTILINE
)
_FULL_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")


class LockfilePinAudit:
    """Check that every dependency is pinned by a lock file.

    A lock file is a dict::

        {"version": 2,
         "target_platform": "linux",
         "packages": [
             {"name": "Django", "version": "4.2.1",
              "source": "https://pypi.org/simple",
              "integrity": "sha256:...",
              "dependencies": ["asgiref"],
              "optional": False, "platforms": ["linux"], "peer": False,
              "git_commit": "40 hex characters"}
         ]}

    ``audit(lockfile, manifest=None)`` accepts a dict or JSON text and returns
    ``{"total", "packages", "unpinned", "source_anomalies", "issues"}``.

      * names are lower-cased and ``_`` becomes ``-``; entries with the same
        normalized name and version are merged, different versions of one
        name stay separate;
      * a registry source must exactly match ``registry_allowlist``;
        ``git+`` sources are accepted only with a 40 character hex
        ``git_commit`` and otherwise report ``git_ref_not_pinned``;
      * a missing or empty ``integrity`` reports ``missing_integrity`` and
        puts the name in ``unpinned``;
      * a ``version`` different from ``lockfile_version`` raises
        ``ValueError``;
      * dependencies are resolved only against packages present in the lock
        file.  ``manifest`` is accepted for compatibility but never used for
        resolution; a dependency that is absent reports
        ``unresolved_dependency``;
      * optional packages are audited only when their ``platforms`` list
        contains the lock file's ``target_platform``; peer dependencies are
        always audited;
      * a text input containing a merge conflict marker, invalid JSON, an
        unsupported lock file version or a package entry that is not a dict
        without ``name``/``version`` raises ``ValueError``.

    ``packages``, ``unpinned`` and ``source_anomalies`` are sorted; issues are
    sorted by ``(code, name)``.
    """

    def __init__(self, registry_allowlist=(), lockfile_version=2):
        self.registry_allowlist = tuple(registry_allowlist)
        self.lockfile_version = lockfile_version

    def audit(self, lockfile, manifest=None):
        data = self._load(lockfile)

        if not isinstance(data, dict):
            raise ValueError("lock file must be a JSON object")
        if data.get("version") != self.lockfile_version:
            raise ValueError(
                "unsupported lock file version: {!r}".format(
                    data.get("version")
                )
            )

        raw_packages = data.get("packages", [])
        if not isinstance(raw_packages, list):
            raise ValueError("lock file packages must be a list")

        merged = {}
        for raw in raw_packages:
            package = self._validate_package(raw)
            key = (package["name"], package["version"])
            if key in merged:
                self._merge_package(merged[key], package)
            else:
                merged[key] = package

        all_names = {package["name"] for package in merged.values()}
        target_platform = data.get("target_platform")

        audited = [
            package
            for package in merged.values()
            if self._is_audited(package, target_platform)
        ]

        issues = set()
        unpinned = set()
        source_anomalies = set()

        for package in audited:
            name = package["name"]
            source = package.get("source")

            if isinstance(source, str) and source.lower().startswith("git+"):
                commit = package.get("git_commit")
                if not isinstance(commit, str) or not _FULL_COMMIT.fullmatch(
                    commit
                ):
                    issues.add(("git_ref_not_pinned", name))
                    unpinned.add(name)
            elif source not in self.registry_allowlist:
                issues.add(("source_not_allowed", name))
                source_anomalies.add(name)

            integrity = package.get("integrity")
            if not isinstance(integrity, str) or not integrity.strip():
                issues.add(("missing_integrity", name))
                unpinned.add(name)

            for dependency in self._dependencies(package):
                if dependency not in all_names:
                    issues.add(("unresolved_dependency", dependency))

        packages = sorted(
            audited, key=lambda package: (package["name"], package["version"])
        )
        issue_list = [
            {"code": code, "name": name}
            for code, name in sorted(issues, key=lambda issue: (issue[0], issue[1]))
        ]

        return {
            "total": len(packages),
            "packages": packages,
            "unpinned": sorted(unpinned),
            "source_anomalies": sorted(source_anomalies),
            "issues": issue_list,
        }

    @staticmethod
    def _load(lockfile):
        if isinstance(lockfile, str):
            if _CONFLICT_MARKER.search(lockfile):
                raise ValueError("lock file contains merge conflict markers")
            try:
                return json.loads(lockfile)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid lock file JSON") from exc
        return lockfile

    @staticmethod
    def _normalize_name(name):
        return name.lower().replace("_", "-")

    def _validate_package(self, raw):
        if not isinstance(raw, dict):
            raise ValueError("package entry must be an object")
        if "name" not in raw or "version" not in raw:
            raise ValueError("package entry requires name and version")

        name = raw["name"]
        version = raw["version"]
        if not isinstance(name, str) or not name:
            raise ValueError("package name must be a non-empty string")
        if not isinstance(version, str) or not version:
            raise ValueError("package version must be a non-empty string")

        package = dict(raw)
        package["name"] = self._normalize_name(name)
        return package

    @staticmethod
    def _merge_package(existing, incoming):
        existing_dependencies = existing.get("dependencies")
        incoming_dependencies = incoming.get("dependencies")
        if isinstance(existing_dependencies, list) and isinstance(
            incoming_dependencies, list
        ):
            existing["dependencies"] = existing_dependencies + [
                dependency
                for dependency in incoming_dependencies
                if dependency not in existing_dependencies
            ]
        elif incoming_dependencies is not None:
            existing["dependencies"] = incoming_dependencies

        existing_platforms = existing.get("platforms")
        incoming_platforms = incoming.get("platforms")
        if isinstance(existing_platforms, list) and isinstance(
            incoming_platforms, list
        ):
            existing["platforms"] = existing_platforms + [
                platform
                for platform in incoming_platforms
                if platform not in existing_platforms
            ]
        elif incoming_platforms is not None:
            existing["platforms"] = incoming_platforms

        if incoming.get("peer"):
            existing["peer"] = True

        if "optional" in existing or "optional" in incoming:
            existing["optional"] = bool(existing.get("optional")) and bool(
                incoming.get("optional")
            )

        for field in ("source", "integrity", "git_commit"):
            if not existing.get(field) and incoming.get(field):
                existing[field] = incoming[field]

    @staticmethod
    def _is_audited(package, target_platform):
        if not package.get("optional"):
            return True
        if package.get("peer"):
            return True
        platforms = package.get("platforms", [])
        if isinstance(platforms, str):
            platforms = [platforms]
        if not isinstance(platforms, list):
            return False
        return target_platform in platforms

    def _dependencies(self, package):
        dependencies = package.get("dependencies", [])
        if dependencies is None:
            return []
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        if not isinstance(dependencies, list):
            raise ValueError("package dependencies must be a list")

        normalized = []
        for dependency in dependencies:
            if not isinstance(dependency, str) or not dependency:
                raise ValueError(
                    "package dependencies must be non-empty strings"
                )
            normalized.append(self._normalize_name(dependency))
        return normalized
