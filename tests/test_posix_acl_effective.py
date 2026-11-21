import pytest

import posix_acl_effective as pae


def acl(*entries):
    return [
        {"tag": tag, "id": ident, "perms": perms}
        for tag, ident, perms in entries
    ]


def find(entries, tag, ident):
    for entry in entries:
        if entry["tag"] == tag and entry["id"] == ident:
            return entry
    raise AssertionError((tag, ident))


def test_named_user_entry_wins_over_group_entry():
    entries = acl(
        ("user", None, "rwx"),
        ("user", "alice", "rw-"),
        ("group", None, "---"),
        ("group", "staff", "r--"),
        ("mask", None, "rw-"),
        ("other", None, "---"),
    )
    result = pae.PosixAclEffective().effective(
        entries, "alice", owner="root", owner_group="root", groups=("staff",)
    )
    assert result["perms"] == "rw"
    assert result["matched"]["tag"] == "user"
    assert result["matched"]["id"] == "alice"


def test_mask_caps_named_user_permissions():
    entries = acl(
        ("user", None, "rwx"),
        ("user", "alice", "rwx"),
        ("group", None, "---"),
        ("mask", None, "r--"),
        ("other", None, "---"),
    )
    result = pae.PosixAclEffective().effective(
        entries, "alice", owner="root", groups=()
    )
    assert result["perms"] == "r"
    assert result["mask"] == "r"


def test_owner_entry_is_not_capped_by_mask():
    entries = acl(
        ("user", None, "rwx"),
        ("group", None, "---"),
        ("mask", None, "---"),
        ("other", None, "---"),
    )
    result = pae.PosixAclEffective().effective(
        entries, "alice", owner="alice", groups=()
    )
    assert result["perms"] == "rwx"
    assert result["mask"] == ""


def test_other_entry_is_never_capped_by_mask():
    entries = acl(
        ("user", None, "rwx"),
        ("group", None, "---"),
        ("mask", None, "---"),
        ("other", None, "r-x"),
    )
    result = pae.PosixAclEffective().effective(
        entries, "bob", owner="alice", owner_group="staff", groups=("dev",)
    )
    assert result["matched"]["tag"] == "other"
    assert result["perms"] == "rx"


def test_owner_match_beats_named_user_entry():
    entries = acl(
        ("user", None, "rwx"),
        ("user", "alice", "---"),
        ("group", None, "---"),
        ("mask", None, "---"),
        ("other", None, "---"),
    )
    result = pae.PosixAclEffective().effective(
        entries, "alice", owner="alice", groups=()
    )
    assert result["perms"] == "rwx"
    assert result["matched"]["tag"] == "user"
    assert result["matched"]["id"] is None


def test_owning_group_entry_applies_before_named_groups():
    entries = acl(
        ("user", None, "rwx"),
        ("group", None, "r-x"),
        ("group", "dev", "rwx"),
        ("mask", None, "rwx"),
        ("other", None, "---"),
    )
    result = pae.PosixAclEffective().effective(
        entries, "bob", owner="alice", owner_group="staff",
        groups=("staff", "dev"),
    )
    assert result["matched"]["tag"] == "group"
    assert result["matched"]["id"] is None
    assert result["perms"] == "rx"


def test_named_supplementary_group_is_used_when_not_in_owning_group():
    entries = acl(
        ("user", None, "rwx"),
        ("group", None, "---"),
        ("group", "dev", "rw-"),
        ("mask", None, "rw-"),
        ("other", None, "---"),
    )
    result = pae.PosixAclEffective().effective(
        entries, "bob", owner="alice", owner_group="staff", groups=("dev",)
    )
    assert result["matched"]["id"] == "dev"
    assert result["perms"] == "rw"


def test_minimal_three_entry_acl_is_classified_as_minimal():
    entries = acl(
        ("user", None, "rwx"),
        ("group", None, "r-x"),
        ("other", None, "---"),
    )
    assert pae.PosixAclEffective().classify(entries) == "minimal"


def test_mask_or_named_entry_makes_acl_extended():
    with_mask = acl(
        ("user", None, "rwx"),
        ("group", None, "r-x"),
        ("mask", None, "r-x"),
        ("other", None, "---"),
    )
    with_named = acl(
        ("user", None, "rwx"),
        ("user", "alice", "r--"),
        ("group", None, "r-x"),
        ("other", None, "---"),
    )
    assert pae.PosixAclEffective().classify(with_mask) == "extended"
    assert pae.PosixAclEffective().classify(with_named) == "extended"


def test_normalize_merges_duplicates_and_ignores_input_order():
    entries = acl(
        ("user", None, "rwx"),
        ("user", None, "r-x"),
        ("user", "alice", "rw-"),
        ("user", "alice", "r--"),
        ("group", None, "r-x"),
        ("other", None, "---"),
    )
    normalized = pae.PosixAclEffective().normalize(entries)
    assert normalized[0]["perms"] == "rx"
    alice = [e for e in normalized if e["tag"] == "user" and e["id"] == "alice"]
    assert len(alice) == 1
    assert alice[0]["perms"] == "r"
    shuffled = pae.PosixAclEffective().normalize(list(reversed(entries)))
    assert shuffled == normalized


def test_directory_search_permission_is_checked_separately():
    entries = acl(
        ("user", None, "r--"),
        ("group", None, "---"),
        ("other", None, "---"),
    )
    evaluator = pae.PosixAclEffective()
    assert evaluator.check(
        entries, "alice", owner="alice", path_kind="dir", wanted="r"
    ) is True
    assert evaluator.check(
        entries, "alice", owner="alice", path_kind="dir", wanted="x"
    ) is False


def test_inheritance_from_default_acl_ignores_umask():
    default = acl(
        ("user", None, "rwx"),
        ("user", "alice", "rw-"),
        ("group", None, "r--"),
        ("mask", None, "rw-"),
        ("other", None, "r--"),
    )
    result = pae.PosixAclEffective().inherit(default, umask=0o077, mode=0o640)
    assert find(result, "user", None)["perms"] == "rw"
    assert find(result, "other", None)["perms"] == ""
    assert find(result, "mask", None)["perms"] == "r"
    assert find(result, "user", "alice")["perms"] == "rw"
    effective = pae.PosixAclEffective().effective(result, "alice", owner="root")
    assert effective["perms"] == "r"


def test_inheritance_without_default_acl_returns_none():
    assert pae.PosixAclEffective().inherit(
        None, umask=0o022, mode=0o666
    ) is None


def test_chmod_updates_mask_and_leaves_group_entry_alone():
    entries = acl(
        ("user", None, "rwx"),
        ("user", "alice", "rw-"),
        ("group", None, "rwx"),
        ("mask", None, "rw-"),
        ("other", None, "rwx"),
    )
    updated = pae.PosixAclEffective().apply_chmod(entries, 0o640)
    assert find(updated, "user", None)["perms"] == "rw"
    assert find(updated, "group", None)["perms"] == "rwx"
    assert find(updated, "mask", None)["perms"] == "r"
    assert find(updated, "other", None)["perms"] == ""
    effective = pae.PosixAclEffective().effective(updated, "alice", owner="root")
    assert effective["perms"] == "r"


def test_tree_walk_chmods_symlink_targets_and_can_reject():
    tree = [
        {
            "path": "data/file",
            "acl": acl(("user", None, "rwx"), ("group", None, "---"),
                       ("other", None, "---")),
            "symlink": None,
        },
        {
            "path": "data/link",
            "acl": acl(("user", None, "r--"), ("group", None, "---"),
                       ("other", None, "---")),
            "symlink": "data/file",
        },
    ]
    result = pae.PosixAclEffective().apply_tree(tree, 0o600)
    assert find(result["data/file"], "user", None)["perms"] == "rw"
    assert find(result["data/link"], "user", None)["perms"] == "r"
    with pytest.raises(PermissionError):
        pae.PosixAclEffective(symlink_policy="reject").apply_tree(tree, 0o600)
