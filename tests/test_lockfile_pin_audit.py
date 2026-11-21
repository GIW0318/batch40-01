import pytest

import lockfile_pin_audit as lpa


REG = "https://pypi.org/simple"


def pkg(name, version="1.0", **overrides):
    entry = {
        "name": name,
        "version": version,
        "source": REG,
        "integrity": "sha256:" + "a" * 64,
    }
    entry.update(overrides)
    return entry


def lock(*packages, version=2, platform="linux"):
    return {
        "version": version,
        "target_platform": platform,
        "packages": list(packages),
    }


def audit(lockfile, manifest=None):
    return lpa.LockfilePinAudit([REG]).audit(lockfile, manifest=manifest)


def codes(result):
    return [issue["code"] for issue in result["issues"]]


def test_names_are_normalized_and_same_version_merges():
    result = audit(lock(pkg("Django_Test"), pkg("django-test")))
    assert result["total"] == 1
    assert result["packages"][0]["name"] == "django-test"


def test_multiple_versions_of_one_package_stay_separate():
    result = audit(lock(pkg("django", "1.0"), pkg("Django", "2.0")))
    assert result["total"] == 2
    assert [entry["version"] for entry in result["packages"]] == ["1.0", "2.0"]


def test_source_must_match_the_allowlist():
    result = audit(lock(pkg("evil", source="https://evil.example/simple")))
    assert result["source_anomalies"] == ["evil"]
    assert "source_not_allowed" in codes(result)


def test_git_source_requires_a_full_commit_id():
    pinned = pkg(
        "gitpkg", source="git+https://github.com/x/y", git_commit="f" * 40
    )
    assert audit(lock(pinned))["issues"] == []
    branch = pkg(
        "gitpkg", source="git+https://github.com/x/y", git_commit="main"
    )
    result = audit(lock(branch))
    assert "git_ref_not_pinned" in codes(result)
    assert result["unpinned"] == ["gitpkg"]


def test_missing_integrity_is_unpinned():
    entry = pkg("requests")
    del entry["integrity"]
    result = audit(lock(entry))
    assert "missing_integrity" in codes(result)
    assert result["unpinned"] == ["requests"]


def test_unsupported_lockfile_version_raises():
    with pytest.raises(ValueError):
        audit(lock(version=99))


def test_conflict_markers_raise():
    text = "<<<<<<< HEAD\n" + '{"version": 2, "packages": []}\n'
    with pytest.raises(ValueError):
        lpa.LockfilePinAudit([REG]).audit(text)


def test_manifest_never_overrides_lockfile_versions():
    lockfile = lock(pkg("asgiref", "3.7.2"))
    manifest = {"packages": [{"name": "asgiref", "version": "9.9.9"}]}
    result = audit(lockfile, manifest=manifest)
    assert result["packages"][0]["version"] == "3.7.2"


def test_dependency_missing_from_lockfile_is_reported():
    result = audit(lock(pkg("django", dependencies=["ghost"])))
    assert "unresolved_dependency" in codes(result)


def test_optional_dependency_is_audited_for_target_platform():
    result = audit(lock(pkg("uvloop", optional=True, platforms=["linux"]),
                        pkg("base")))
    assert result["total"] == 2
    assert "uvloop" in [entry["name"] for entry in result["packages"]]


def test_optional_dependency_for_another_platform_is_skipped():
    result = audit(lock(pkg("pywin32", optional=True, platforms=["win32"]),
                        pkg("base")))
    assert result["total"] == 1
    assert "pywin32" not in [entry["name"] for entry in result["packages"]]


def test_peer_dependency_is_always_audited():
    result = audit(lock(pkg("peerpkg", optional=True, peer=True,
                            platforms=["win32"]), pkg("base")))
    assert result["total"] == 2


def test_dependency_names_are_normalized_for_resolution():
    result = audit(lock(pkg("django", dependencies=["Foo_Bar"]),
                        pkg("foo-bar")))
    assert "unresolved_dependency" not in codes(result)


def test_valid_lockfile_reports_clean_statistics():
    result = audit(lock(pkg("django"), pkg("asgiref")))
    assert result["total"] == 2
    assert result["unpinned"] == []
    assert result["source_anomalies"] == []
    assert result["issues"] == []


def test_bad_package_entry_raises():
    with pytest.raises(ValueError):
        audit({"version": 2, "target_platform": "linux", "packages": ["junk"]})
