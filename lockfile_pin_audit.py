"""Audit a lock file and rebuild the dependency graph only from it."""


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
        raise NotImplementedError
