import pytest

import seccomp_profile_merge as spm

ALLOW = "SCMP_ACT_ALLOW"
ERRNO = "SCMP_ACT_ERRNO"
NOTIFY = "SCMP_ACT_NOTIFY"


def merger():
    return spm.SeccompProfileMerge()


def profile(text):
    return merger().parse(text)


def test_parse_handles_comments_and_includes_line_by_line():
    text = "\n".join([
        "# top level comment",
        "default = SCMP_ACT_ERRNO",
        "arch x86_64",
        "allow read  # inline comment",
        '#include "extra"',
    ])
    parsed = merger().parse(text, includes={
        "extra": "arch x86_64\nallow write\n",
    })
    section = parsed["arches"]["x86_64"]["syscalls"]
    assert parsed["default"] == ERRNO
    assert section["read"]["action"] == ALLOW
    assert section["write"]["action"] == ALLOW


def test_arch_aliases_collapse_into_one_section():
    parsed = profile("\n".join([
        "default = SCMP_ACT_ERRNO",
        "arch amd64",
        "allow read",
        "arch x86_64",
        "allow write",
    ]))
    assert list(parsed["arches"]) == ["x86_64"]
    assert set(parsed["arches"]["x86_64"]["syscalls"]) == {"read", "write"}


def test_merge_takes_the_stricter_default_action():
    base = profile("default = SCMP_ACT_ALLOW\narch x86_64\nallow read\n")
    overlay = profile("default = SCMP_ACT_ERRNO\narch x86_64\nallow read\n")
    merged = merger().merge(base, overlay)
    assert merged["default"] == ERRNO


def test_merge_takes_the_stricter_per_syscall_action():
    base = profile("default = SCMP_ACT_ALLOW\narch x86_64\nerrno EACCES read\n")
    overlay = profile("default = SCMP_ACT_ALLOW\narch x86_64\nallow read\n")
    merged = merger().merge(base, overlay)
    entry = merged["arches"]["x86_64"]["syscalls"]["read"]
    assert entry["action"] == ERRNO


def test_conflicting_errno_codes_keep_base_and_record_conflict():
    base = profile("default = SCMP_ACT_ALLOW\narch x86_64\nerrno EACCES read\n")
    overlay = profile("default = SCMP_ACT_ALLOW\narch x86_64\nerrno EPERM read\n")
    merged = merger().merge(base, overlay)
    entry = merged["arches"]["x86_64"]["syscalls"]["read"]
    assert entry["action"] == ERRNO
    assert entry["errno"] == "EACCES"
    assert merged["conflicts"]


def test_duplicate_syscall_inside_one_profile_is_recorded():
    parsed = profile("\n".join([
        "default = SCMP_ACT_ALLOW",
        "arch x86_64",
        "allow read",
        "errno EPERM read",
    ]))
    entry = parsed["arches"]["x86_64"]["syscalls"]["read"]
    assert entry["action"] == ERRNO
    assert entry["errno"] == "EPERM"
    assert parsed["conflicts"]


def test_argument_operator_semantics_are_preserved():
    parsed = profile("\n".join([
        "default = SCMP_ACT_ERRNO",
        "arch x86_64",
        "allow openat if arg1 >= 4",
    ]))
    conditions = parsed["arches"]["x86_64"]["syscalls"]["openat"]["args"]
    assert conditions == [{"index": 1, "op": "SCMP_CMP_GE", "value": 4}]


def test_merged_argument_filters_are_combined_with_and():
    base = profile("\n".join([
        "default = SCMP_ACT_ERRNO",
        "arch x86_64",
        "allow openat if arg1 >= 4",
    ]))
    overlay = profile("\n".join([
        "default = SCMP_ACT_ERRNO",
        "arch x86_64",
        "allow openat if arg1 <= 8",
    ]))
    m = merger()
    merged = m.merge(base, overlay)
    entry = merged["arches"]["x86_64"]["syscalls"]["openat"]
    ops = sorted(condition["op"] for condition in entry["args"])
    assert ops == ["SCMP_CMP_GE", "SCMP_CMP_LE"]
    assert m.decide(merged, "x86_64", "openat", {"arg1": 5}) == ALLOW
    assert m.decide(merged, "x86_64", "openat", {"arg1": 2}) == ERRNO
    assert m.decide(merged, "x86_64", "openat", {"arg1": 9}) == ERRNO


def test_arch_sections_stay_independent():
    base = profile("default = SCMP_ACT_ERRNO\narch x86_64\nallow read\n")
    overlay = profile("default = SCMP_ACT_ERRNO\narch x86\nallow write\n")
    merged = merger().merge(base, overlay)
    assert set(merged["arches"]["x86_64"]["syscalls"]) == {"read"}
    assert set(merged["arches"]["x86"]["syscalls"]) == {"write"}


def test_syscall_numbers_are_resolved_per_architecture():
    m = merger()
    p64 = m.parse("default = SCMP_ACT_ERRNO\narch x86_64\nallow 0\n")
    p32 = m.parse("default = SCMP_ACT_ERRNO\narch x86\nallow 3\n")
    assert "read" in p64["arches"]["x86_64"]["syscalls"]
    assert "read" in p32["arches"]["x86"]["syscalls"]
    close_profile = m.parse("default = SCMP_ACT_ERRNO\narch x86_64\nallow 3\n")
    assert "close" in close_profile["arches"]["x86_64"]["syscalls"]
    merged = m.merge(p64, p32)
    assert "read" in merged["arches"]["x86_64"]["syscalls"]
    assert "read" in merged["arches"]["x86"]["syscalls"]


def test_unknown_syscall_is_rejected():
    with pytest.raises(ValueError):
        profile("\n".join([
            "default = SCMP_ACT_ALLOW",
            "arch x86_64",
            "allow definitely_not_a_syscall",
        ]))


def test_notify_action_requires_explicit_opt_in():
    base = profile("default = SCMP_ACT_ALLOW\narch x86_64\nallow ptrace\n")
    overlay = profile("default = SCMP_ACT_ALLOW\narch x86_64\nnotify ptrace\n")
    with pytest.raises(ValueError):
        merger().merge(base, overlay)


def test_notify_is_stricter_than_allow_and_flagged():
    base = profile("default = SCMP_ACT_ALLOW\narch x86_64\nallow ptrace\n")
    overlay = profile("default = SCMP_ACT_ALLOW\narch x86_64\nnotify ptrace\n")
    m = merger()
    merged = m.merge(base, overlay, allow_notify=True)
    entry = merged["arches"]["x86_64"]["syscalls"]["ptrace"]
    assert entry["action"] == NOTIFY
    assert merged["notify"]
    assert m.decide(merged, "x86_64", "ptrace") == NOTIFY


def test_merge_applies_the_other_default_to_unlisted_syscalls():
    base = profile("default = SCMP_ACT_ERRNO\narch x86_64\nallow read\n")
    overlay = profile("default = SCMP_ACT_ERRNO\narch x86_64\nallow write\n")
    m = merger()
    merged = m.merge(base, overlay)
    assert merged["arches"]["x86_64"]["syscalls"]["read"]["action"] == ERRNO
    assert merged["arches"]["x86_64"]["syscalls"]["write"]["action"] == ERRNO
    assert m.decide(merged, "x86_64", "read") == ERRNO
    assert m.decide(merged, "x86_64", "write") == ERRNO


def test_arch_alias_sections_merge_without_duplication():
    base = profile("\n".join([
        "default = SCMP_ACT_ALLOW",
        "arch SCMP_ARCH_X86_64",
        "allow read",
    ]))
    overlay = profile("\n".join([
        "default = SCMP_ACT_ALLOW",
        "arch amd64",
        "allow write",
    ]))
    merged = merger().merge(base, overlay)
    assert list(merged["arches"]) == ["x86_64"]
    section = merged["arches"]["x86_64"]["syscalls"]
    assert section["read"]["action"] == ALLOW
    assert section["write"]["action"] == ALLOW
