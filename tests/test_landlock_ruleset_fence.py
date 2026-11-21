import pytest

import landlock_ruleset_fence as lrf


def fence(**kwargs):
    return lrf.LandlockRulesetFence(abi=3, **kwargs)


def make_ruleset(f, rights=("read_file",), abi=None):
    return f.create_ruleset(set(rights), abi=abi)["id"]


def restrict_with(f, rules, pid=10):
    ruleset = make_ruleset(f, [rule[1] for rule in rules])
    for path, right, allow in rules:
        f.add_rule(ruleset, path, {right}, allow=allow)
    f.restrict(ruleset, pid)
    return ruleset


def test_rights_are_negotiated_per_abi():
    f = fence()
    abi1 = f.rights_for_abi(1)
    abi2 = f.rights_for_abi(2)
    abi3 = f.rights_for_abi(3)
    assert "read_file" in abi1
    assert "refer" not in abi1 and "truncate" not in abi1
    assert "refer" in abi2 and "truncate" not in abi2
    assert "refer" in abi3 and "truncate" in abi3


def test_unsupported_rights_are_rejected_not_silently_masked():
    f = fence()
    with pytest.raises(ValueError):
        f.create_ruleset({"read_file", "refer"}, abi=1)
    with pytest.raises(ValueError):
        f.create_ruleset({"read_file", "truncate"}, abi=2)
    with pytest.raises(ValueError):
        f.create_ruleset(set())


def test_empty_ruleset_cannot_be_enforced():
    f = fence()
    ruleset = make_ruleset(f)
    with pytest.raises(ValueError):
        f.restrict(ruleset, 10)


def test_relative_rule_path_is_rejected():
    f = fence()
    ruleset = make_ruleset(f)
    with pytest.raises(ValueError):
        f.add_rule(ruleset, "srv/data", {"read_file"})


def test_overlong_rule_path_is_rejected_not_truncated():
    f = fence(max_path=16)
    ruleset = make_ruleset(f)
    with pytest.raises(ValueError):
        f.add_rule(ruleset, "/srv/" + "a" * 32, {"read_file"})


def test_resolve_path_normalizes_relative_and_dot_segments():
    f = fence()
    assert f.resolve_path("a/../b", cwd="/srv") == "/srv/b"
    assert f.resolve_path("/srv/./x//y") == "/srv/x/y"
    assert f.resolve_path("../etc", cwd="/srv/app") == "/srv/etc"


def test_directory_rule_allows_the_whole_subtree():
    f = fence()
    restrict_with(f, [("/srv", "read_file", True)])
    assert f.check(10, "/srv/data/report", {"read_file"}) is True
    assert f.check(10, "/srv", {"read_file"}) is True
    assert f.check(10, "/etc/passwd", {"read_file"}) is False


def test_specific_deny_wins_regardless_of_insertion_order():
    f = fence()
    allow_first = make_ruleset(f)
    f.add_rule(allow_first, "/srv", {"read_file"}, allow=True)
    f.add_rule(allow_first, "/srv/secret", {"read_file"}, allow=False)
    f.restrict(allow_first, 10)
    deny_first = make_ruleset(f)
    f.add_rule(deny_first, "/srv/secret", {"read_file"}, allow=False)
    f.add_rule(deny_first, "/srv", {"read_file"}, allow=True)
    f.restrict(deny_first, 11)
    for pid in (10, 11):
        assert f.check(pid, "/srv/public", {"read_file"}) is True
        assert f.check(pid, "/srv/secret/key", {"read_file"}) is False


def test_file_rule_beats_broader_directory_rule():
    f = fence()
    ruleset = make_ruleset(f, ["write_file"])
    f.add_rule(ruleset, "/srv", {"write_file"}, kind="dir", allow=False)
    f.add_rule(ruleset, "/srv/data.txt", {"write_file"}, kind="file", allow=True)
    f.restrict(ruleset, 10)
    assert f.check(10, "/srv/data.txt", {"write_file"}) is True
    assert f.check(10, "/srv/other.txt", {"write_file"}) is False


def test_symlink_inside_allowed_directory_is_resolved():
    f = fence(symlinks={"/srv/link": "/etc/passwd"})
    restrict_with(f, [("/srv", "read_file", True)])
    assert f.check(10, "/srv/plain.txt", {"read_file"}) is True
    assert f.check(10, "/srv/link", {"read_file"}) is False


def test_bind_mount_alias_is_resolved_before_matching():
    f = fence(mounts={"/mnt/alias": "/srv"})
    ruleset = make_ruleset(f)
    f.add_rule(ruleset, "/srv/data.txt", {"read_file"}, kind="file")
    f.restrict(ruleset, 10)
    assert f.check(10, "/mnt/alias/data.txt", {"read_file"}) is True
    assert f.check(10, "/mnt/alias/other.txt", {"read_file"}) is False


def test_child_process_inherits_parent_fence():
    f = fence()
    restrict_with(f, [("/srv", "read_file", True)])
    f.spawn(10, 11)
    f.spawn(20, 21)
    assert f.check(11, "/srv/data", {"read_file"}) is True
    assert f.check(11, "/etc/shadow", {"read_file"}) is False
    assert f.check(21, "/etc/shadow", {"read_file"}) is True


def test_rules_apply_only_after_restrict_stage():
    f = fence()
    ruleset = make_ruleset(f)
    f.add_rule(ruleset, "/srv", {"read_file"})
    assert f.check(10, "/etc/passwd", {"read_file"}) is True
    f.restrict(ruleset, 10)
    assert f.check(10, "/srv/data", {"read_file"}) is True
    assert f.check(10, "/etc/passwd", {"read_file"}) is False


def test_unhandled_access_right_is_an_explicit_error():
    f = fence()
    ruleset = make_ruleset(f, ["read_file"])
    with pytest.raises(ValueError):
        f.add_rule(ruleset, "/srv", {"write_file"})
    f.add_rule(ruleset, "/srv", {"read_file"})
    f.restrict(ruleset, 10)
    with pytest.raises(ValueError):
        f.check(10, "/srv/data", {"write_file"})


def test_handled_but_not_granted_right_is_denied():
    f = fence()
    ruleset = make_ruleset(f, ["read_file", "write_file"])
    f.add_rule(ruleset, "/srv", {"read_file"})
    f.restrict(ruleset, 10)
    assert f.check(10, "/srv/data", {"read_file"}) is True
    assert f.check(10, "/srv/data", {"write_file"}) is False
