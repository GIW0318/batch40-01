import pytest

import userns_idmap_check as uic


def spec(**overrides):
    base = {
        "owner": "alice",
        "uid_map": [[0, 100000, 65536]],
        "gid_map": [[0, 100000, 65536]],
        "setgroups": "deny",
    }
    base.update(overrides)
    return base


def codes(result):
    return [issue["code"] for issue in result["issues"]]


def test_valid_spec_passes_without_issues():
    result = uic.UsernsIdmapCheck().validate(
        spec(uid_map=[[0, 100000, 65536], [65536, 200000, 100]])
    )
    assert result["ok"] is True
    assert result["issues"] == []
    assert result["uid_map"] == [[0, 100000, 65536], [65536, 200000, 100]]
    assert result["gid_map"] == [[0, 100000, 65536]]


def test_overlapping_user_ranges_are_rejected():
    result = uic.UsernsIdmapCheck().validate(
        spec(uid_map=[[0, 100000, 65536], [65535, 200000, 10]])
    )
    assert result["ok"] is False
    assert "overlap" in codes(result)


def test_overlapping_group_ranges_are_rejected():
    result = uic.UsernsIdmapCheck().validate(
        spec(gid_map=[[0, 100000, 100], [50, 100100, 100]])
    )
    assert result["ok"] is False
    assert "overlap" in codes(result)


def test_host_root_uid_is_rejected_by_default():
    result = uic.UsernsIdmapCheck().validate(
        spec(uid_map=[[0, 0, 1]], gid_map=[[0, 100000, 1]])
    )
    assert result["ok"] is False
    assert "host_root" in codes(result)


def test_host_root_is_accepted_when_explicitly_allowed():
    check = uic.UsernsIdmapCheck(allow_host_root=True)
    result = check.validate(spec(uid_map=[[0, 0, 1]], gid_map=[[0, 100000, 1]]))
    assert result["ok"] is True
    assert result["issues"] == []


def test_missing_group_map_is_reported():
    broken = spec()
    del broken["gid_map"]
    result = uic.UsernsIdmapCheck().validate(broken)
    assert result["ok"] is False
    assert "missing_gid_map" in codes(result)


def test_setgroups_must_be_denied_before_group_map():
    result = uic.UsernsIdmapCheck().validate(spec(setgroups="allow"))
    assert result["ok"] is False
    assert "setgroups_enabled" in codes(result)


def test_subordinate_ranges_may_not_overlap_across_owners():
    result = uic.UsernsIdmapCheck().validate(
        spec(subordinate={"alice": [[100000, 65536]], "bob": [[150000, 10]]})
    )
    assert result["ok"] is False
    assert "subordinate_overlap" in codes(result)


def test_too_many_lines_are_rejected_and_extras_reported():
    check = uic.UsernsIdmapCheck(max_lines=2)
    uid_map = [[0, 100000, 1], [1, 100001, 1], [2, 100002, 1], [3, 100003, 1]]
    result = check.validate(spec(uid_map=uid_map, gid_map=[[0, 100000, 1]]))
    assert result["ok"] is False
    assert "too_many_lines" in codes(result)
    assert len(result["extras"]) == 2


def test_range_overflow_is_rejected():
    result = uic.UsernsIdmapCheck().validate(
        spec(uid_map=[[2 ** 32 - 10, 100000, 20]], gid_map=[[0, 100000, 1]])
    )
    assert result["ok"] is False
    assert "overflow" in codes(result)


def test_unsorted_map_is_reported_instead_of_silently_reordered():
    result = uic.UsernsIdmapCheck().validate(
        spec(uid_map=[[65536, 200000, 10], [0, 100000, 10]], gid_map=[[0, 100000, 1]])
    )
    assert result["ok"] is False
    assert "unsorted" in codes(result)


def test_empty_maps_are_not_treated_as_default_mapping():
    result = uic.UsernsIdmapCheck().validate(spec(uid_map=[], gid_map=[]))
    assert result["ok"] is False
    assert "empty_map" in codes(result)


def test_helper_args_reject_bad_line_shapes():
    result = uic.UsernsIdmapCheck().validate_helper_args(
        [[0, 100000], [0, -1, 10], ["a", 1, 2]]
    )
    assert result["ok"] is False
    assert "bad_entry" in codes(result)


def test_helper_args_reject_overflow_and_too_many_lines():
    result = uic.UsernsIdmapCheck(max_lines=2).validate_helper_args(
        [[0, 100000, 2 ** 32]]
    )
    assert result["ok"] is False
    assert "overflow" in codes(result)
    result = uic.UsernsIdmapCheck(max_lines=2).validate_helper_args(
        [[0, 100000, 1], [1, 100001, 1], [2, 100002, 1]]
    )
    assert result["ok"] is False
    assert "too_many_lines" in codes(result)


def test_helper_args_normalize_valid_lines():
    result = uic.UsernsIdmapCheck().validate_helper_args(
        [[0, 100000, 65536], [65536, 200000, 10]]
    )
    assert result["ok"] is True
    assert result["issues"] == []
    assert result["entries"] == [[0, 100000, 65536], [65536, 200000, 10]]
