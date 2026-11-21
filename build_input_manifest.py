"""Normalized manifest of every input that contributes to a build."""

import datetime
import hashlib
import json
import posixpath
import re


_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_VERSION_RANGE_RE = re.compile(r"[\^~*><=| \t]")


class BuildInputManifest:
    """Collect build inputs and expose one deterministic manifest/digest.

    ``__init__(source_date_epoch=0, host_locale="C", host_timezone="UTC",
    root="/build")`` fixes the timestamp (from Source Date Epoch), forces the
    recorded locale to ``"C"`` and timezone to ``"UTC"`` regardless of the
    host values, and treats ``root`` as the build sandbox prefix that is
    stripped from every recorded path.

    ``add_source(name, commit, submodules=None)`` records a source checkout.
    ``commit`` must be a 40 character hex id, otherwise ``issues`` gains
    ``unpinned_source``.  ``submodules`` maps a path to a commit; every
    submodule commit is validated and participates in the digest.

    ``add_toolchain(name, version, digest=None)`` requires an exact version
    (no ``^ ~ * > < = |`` or spaces) and a ``sha256:`` digest; otherwise
    ``issues`` gains ``unpinned_toolchain``.

    ``set_env(env, allowed=())`` keeps only the whitelisted environment
    variables, so proxies and temporary directories cannot perturb the build.

    ``add_file(path, content=b"", symlink_to=None)`` records a file.  Paths
    are normalized (backslashes to slashes, ``root`` stripped, ``.``/``..``
    resolved) and a path that escapes the root reports ``path_escape`` and is
    dropped.  ``content`` is hashed with sha256.  When ``symlink_to`` is
    given the link target is recorded instead of content and the link is not
    dereferenced.

    ``add_generated(path, content, step)`` records a generated file and its
    producing step; an empty or missing step raises ``ValueError``.

    ``add_base_image(ref, digest=None)`` requires a ``sha256:`` digest,
    otherwise ``issues`` gains ``unpinned_base_image``.

    ``add_args(args)`` accepts ``"name=value"`` strings.  Arguments are
    recorded sorted by name, so argument order never changes the digest.

    ``manifest()`` returns a fresh dict with ``sources``, ``toolchains``,
    ``files`` and ``args`` sorted (by name / path), ``env`` sorted by key,
    ``base_image`` (or None) and ``normalized`` =
    ``{"timestamp", "locale": "C", "timezone": "UTC"}``.  ``digest()`` is the
    sha256 of the canonical JSON rendering of ``manifest()``.
    """

    def __init__(self, source_date_epoch=0, host_locale="C",
                 host_timezone="UTC", root="/build"):
        self.source_date_epoch = source_date_epoch
        self.host_locale = host_locale
        self.host_timezone = host_timezone
        self.root = str(root).replace("\\", "/")
        self.issues = []
        self._sources = []
        self._toolchains = []
        self._files = {}
        self._env = {}
        self._base_image = None
        self._args = {}

    @staticmethod
    def _sha256(content):
        if isinstance(content, str):
            content = content.encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def _issue(self, code, **context):
        entry = {"code": code}
        entry.update(context)
        self.issues.append(entry)

    def add_source(self, name, commit, submodules=None):
        submodule_entries = {}
        pinned = bool(_COMMIT_RE.match(str(commit)))
        if not pinned:
            self._issue("unpinned_source", name=name, commit=commit)
        for path, submodule_commit in (submodules or {}).items():
            if not _COMMIT_RE.match(str(submodule_commit)):
                self._issue(
                    "unpinned_source",
                    name=name,
                    path=path,
                    commit=submodule_commit,
                )
            submodule_entries[path] = str(submodule_commit)
        self._sources.append(
            {
                "name": name,
                "commit": str(commit),
                "submodules": submodule_entries,
            }
        )

    def add_toolchain(self, name, version, digest=None):
        exact_version = (
            version is not None
            and str(version) != ""
            and not _VERSION_RANGE_RE.search(str(version))
        )
        checksummed = bool(
            isinstance(digest, str)
            and digest.startswith("sha256:")
            and len(digest) == len("sha256:") + 64
        )
        if not exact_version or not checksummed:
            self._issue("unpinned_toolchain", name=name, version=version)
        self._toolchains.append(
            {
                "name": name,
                "version": version,
                "digest": digest,
            }
        )

    def set_env(self, env, allowed=()):
        allowed = set(allowed)
        self._env = {
            key: value for key, value in env.items() if key in allowed
        }

    def _normalize_path(self, path):
        normalized = str(path).replace("\\", "/")
        if posixpath.isabs(normalized):
            root = posixpath.normpath(self.root)
            normalized = posixpath.normpath(normalized)
            root_with_sep = root.rstrip("/") + "/"
            if normalized == root:
                return ""
            if normalized.startswith(root_with_sep):
                return normalized[len(root_with_sep):]
            return None
        normalized = posixpath.normpath(normalized)
        if normalized == ".." or normalized.startswith("../"):
            return None
        return normalized

    def add_file(self, path, content=b"", symlink_to=None):
        normalized = self._normalize_path(path)
        if normalized is None:
            self._issue("path_escape", path=path)
            return
        if symlink_to is not None:
            entry = {"path": normalized, "symlink_to": symlink_to}
        else:
            entry = {
                "path": normalized,
                "sha256": self._sha256(content),
            }
        self._files[normalized] = entry

    def add_generated(self, path, content, step):
        if not step:
            raise ValueError("generated files require a producing step")
        normalized = self._normalize_path(path)
        if normalized is None:
            self._issue("path_escape", path=path)
            return
        self._files[normalized] = {
            "path": normalized,
            "sha256": self._sha256(content),
            "generated_by": step,
        }

    def add_base_image(self, ref, digest=None):
        if not (isinstance(digest, str) and digest.startswith("sha256:")):
            self._issue("unpinned_base_image", ref=ref)
        self._base_image = {"ref": ref, "digest": digest}

    def add_args(self, args):
        for arg in args:
            name, separator, value = str(arg).partition("=")
            if not separator:
                value = ""
            self._args[name] = value

    def manifest(self):
        sources = []
        for source in sorted(self._sources, key=lambda item: item["name"]):
            sources.append(
                {
                    "name": source["name"],
                    "commit": source["commit"],
                    "submodules": [
                        {"path": path, "commit": commit}
                        for path, commit in sorted(
                            source["submodules"].items()
                        )
                    ],
                }
            )
        timestamp = (
            datetime.datetime.fromtimestamp(
                self.source_date_epoch,
                tz=datetime.timezone.utc,
            )
            .replace(microsecond=0)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        return {
            "sources": sources,
            "toolchains": [
                dict(toolchain)
                for toolchain in sorted(
                    self._toolchains, key=lambda item: item["name"]
                )
            ],
            "files": [
                dict(entry)
                for entry in (
                    self._files[key] for key in sorted(self._files)
                )
            ],
            "env": dict(sorted(self._env.items())),
            "base_image": (
                dict(self._base_image) if self._base_image else None
            ),
            "args": [
                {"name": name, "value": value}
                for name, value in sorted(self._args.items())
            ],
            "normalized": {
                "timestamp": timestamp,
                "locale": "C",
                "timezone": "UTC",
            },
        }

    def digest(self):
        canonical = json.dumps(
            self.manifest(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
