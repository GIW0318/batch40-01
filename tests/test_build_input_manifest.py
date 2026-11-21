import hashlib

import pytest

import build_input_manifest as bim


HEX = "a" * 40


def base_manifest(**kwargs):
    manifest = bim.BuildInputManifest(**kwargs)
    manifest.add_source("app", HEX)
    manifest.add_toolchain("python", "3.12.4", "sha256:" + "b" * 64)
    manifest.add_file("src/main.c", b"int main(void) { return 0; }\n")
    manifest.add_base_image("registry.example/app:1", "sha256:" + "c" * 64)
    return manifest


def codes(manifest):
    return [issue["code"] for issue in manifest.issues]


def test_identical_inputs_produce_identical_digests():
    first = base_manifest().digest()
    second = base_manifest().digest()
    assert first == second
    assert len(first) == 64


def test_host_locale_and_timezone_are_normalized_away():
    first = base_manifest(
        host_locale="zh_CN.UTF-8", host_timezone="Asia/Shanghai"
    )
    second = base_manifest(
        host_locale="en_US.UTF-8", host_timezone="America/New_York"
    )
    assert first.manifest()["normalized"]["locale"] == "C"
    assert first.manifest()["normalized"]["timezone"] == "UTC"
    assert first.digest() == second.digest()


def test_timestamp_comes_from_source_date_epoch():
    manifest = bim.BuildInputManifest(source_date_epoch=1700000000)
    assert manifest.manifest()["normalized"]["timestamp"] == "2023-11-14T22:13:20Z"
    epoch = bim.BuildInputManifest(source_date_epoch=0)
    assert epoch.manifest()["normalized"]["timestamp"] == "1970-01-01T00:00:00Z"


def test_file_order_does_not_change_the_digest():
    first = bim.BuildInputManifest()
    first.add_file("src/b.c", b"b")
    first.add_file("src/a.c", b"a")
    second = bim.BuildInputManifest()
    second.add_file("src/a.c", b"a")
    second.add_file("src/b.c", b"b")
    assert first.digest() == second.digest()
    assert [f["path"] for f in first.manifest()["files"]] == [
        "src/a.c", "src/b.c"
    ]


def test_absolute_build_root_is_stripped():
    manifest = bim.BuildInputManifest(root="/build")
    manifest.add_file("/build/src/a.c", b"a")
    assert manifest.manifest()["files"][0]["path"] == "src/a.c"


def test_path_escaping_the_root_is_rejected():
    manifest = bim.BuildInputManifest(root="/build")
    manifest.add_file("/build/../etc/passwd", b"x")
    assert codes(manifest) == ["path_escape"]
    assert manifest.manifest()["files"] == []


def test_submodule_commit_participates_in_the_digest():
    first = base_manifest()
    first.add_source("lib", HEX, submodules={"vendor/z": "d" * 40})
    second = base_manifest()
    second.add_source("lib", HEX, submodules={"vendor/z": "e" * 40})
    assert first.digest() != second.digest()
    assert first.manifest()["sources"][1]["submodules"] == [
        {"path": "vendor/z", "commit": "d" * 40}
    ]


def test_short_source_commit_is_unpinned():
    manifest = bim.BuildInputManifest()
    manifest.add_source("app", "abc123")
    assert "unpinned_source" in codes(manifest)


def test_toolchain_requires_exact_version_and_checksum():
    ranged = bim.BuildInputManifest()
    ranged.add_toolchain("gcc", "^13.2", "sha256:" + "d" * 64)
    assert "unpinned_toolchain" in codes(ranged)
    unchecked = bim.BuildInputManifest()
    unchecked.add_toolchain("gcc", "13.2.1")
    assert "unpinned_toolchain" in codes(unchecked)
    pinned = bim.BuildInputManifest()
    pinned.add_toolchain("gcc", "13.2.1", "sha256:" + "d" * 64)
    assert pinned.issues == []
    assert pinned.manifest()["toolchains"][0]["version"] == "13.2.1"
    assert pinned.manifest()["toolchains"][0]["digest"] == "sha256:" + "d" * 64


def test_environment_is_filtered_by_whitelist():
    manifest = bim.BuildInputManifest()
    manifest.set_env(
        {"CC": "gcc", "HTTPS_PROXY": "http://proxy", "TMPDIR": "/tmp"},
        allowed=("CC",),
    )
    assert manifest.manifest()["env"] == {"CC": "gcc"}
    assert "HTTPS_PROXY" not in manifest.manifest()["env"]


def test_generated_file_without_step_raises():
    manifest = bim.BuildInputManifest()
    with pytest.raises(ValueError):
        manifest.add_generated("gen/parser.c", b"x", "")
    with pytest.raises(ValueError):
        manifest.add_generated("gen/parser.c", b"x", None)


def test_generated_file_records_its_step():
    manifest = bim.BuildInputManifest()
    manifest.add_generated("gen/parser.c", b"x", "bison")
    entry = manifest.manifest()["files"][0]
    assert entry["generated_by"] == "bison"
    assert entry["sha256"] == hashlib.sha256(b"x").hexdigest()


def test_symlink_semantics_are_preserved():
    manifest = bim.BuildInputManifest()
    manifest.add_file("bin/tool", symlink_to="tool-real")
    entry = manifest.manifest()["files"][0]
    assert entry["symlink_to"] == "tool-real"
    assert "sha256" not in entry
    regular = bim.BuildInputManifest()
    regular.add_file("bin/tool", b"tool-real")
    assert manifest.digest() != regular.digest()


def test_base_image_must_be_pinned_to_a_digest():
    tag_only = bim.BuildInputManifest()
    tag_only.add_base_image("registry.example/app:1")
    assert "unpinned_base_image" in codes(tag_only)
    pinned = bim.BuildInputManifest()
    pinned.add_base_image("registry.example/app:1", "sha256:" + "c" * 64)
    assert "unpinned_base_image" not in codes(pinned)
    assert pinned.manifest()["base_image"]["digest"] == "sha256:" + "c" * 64


def test_build_arguments_are_sorted_by_name():
    first = bim.BuildInputManifest()
    first.add_args(["O2=1", "arch=arm64"])
    second = bim.BuildInputManifest()
    second.add_args(["arch=arm64", "O2=1"])
    assert first.digest() == second.digest()
    assert first.manifest()["args"] == [
        {"name": "O2", "value": "1"},
        {"name": "arch", "value": "arm64"},
    ]
