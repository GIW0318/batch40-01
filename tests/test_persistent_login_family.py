import json

import persistent_login_family as plf

DAY = 86400


def store(**kwargs):
    return plf.PersistentLoginFamily(**kwargs)


def login(store, user="alice", device="laptop-1", now=0):
    return store.issue(user, device, now=now)


def test_issue_uses_high_entropy_family_identifier():
    s = store()
    first = login(s)
    second = s.issue("bob", "desk-1", now=0)
    assert first["token"]
    assert first["family_id"] != second["family_id"]
    assert len(first["family_id"]) >= 32
    assert int(first["family_id"], 16) >= 0


def test_validate_accepts_same_device():
    s = store()
    issued = login(s)
    result = s.validate(issued["token"], "laptop-1", now=1)
    assert result["valid"] is True
    assert result["family_id"] == issued["family_id"]


def test_validate_rejects_copied_token_on_other_device():
    s = store()
    issued = login(s)
    result = s.validate(issued["token"], "phone-9", now=1)
    assert result["valid"] is False
    assert result["reason"] == "device mismatch"


def test_validate_rejects_token_for_other_user():
    s = store()
    issued = login(s)
    result = s.validate(issued["token"], "laptop-1", now=1, user="bob")
    assert result["valid"] is False
    assert result["reason"] == "wrong user"


def test_rotation_replaces_token_and_invalidates_old_after_grace():
    s = store()
    issued = login(s)
    rotated = s.rotate(issued["token"], "laptop-1", now=0)
    assert rotated["status"] == "rotated"
    assert rotated["token"] != issued["token"]
    old = s.validate(issued["token"], "laptop-1", now=6)
    assert old["valid"] is False


def test_rotation_within_transition_grace_does_not_fork_chain():
    s = store()
    issued = login(s)
    rotated = s.rotate(issued["token"], "laptop-1", now=0)
    retried = s.rotate(issued["token"], "laptop-1", now=3)
    assert retried["status"] == "grace"
    assert retried["token"] == issued["token"]
    assert s.validate(issued["token"], "laptop-1", now=4)["valid"] is True
    assert s.validate(rotated["token"], "laptop-1", now=4)["valid"] is True


def test_reuse_after_grace_revokes_whole_family():
    s = store()
    issued = login(s)
    rotated = s.rotate(issued["token"], "laptop-1", now=0)
    theft = s.rotate(issued["token"], "laptop-1", now=10)
    assert theft["status"] == "theft"
    assert s.validate(rotated["token"], "laptop-1", now=10)["valid"] is False
    assert s.validate(issued["token"], "laptop-1", now=10)["valid"] is False


def test_logout_revokes_only_the_named_family():
    s = store()
    first = login(s, device="laptop-1")
    second = s.issue("alice", "tablet-2", now=0)
    result = s.logout("alice", first["family_id"])
    assert result["revoked"] == 1
    assert s.validate(first["token"], "laptop-1", now=1)["valid"] is False
    assert s.validate(second["token"], "tablet-2", now=1)["valid"] is True


def test_logout_without_family_revokes_all_user_families():
    s = store()
    first = login(s, device="laptop-1")
    second = s.issue("alice", "tablet-2", now=0)
    result = s.logout("alice")
    assert result["revoked"] == 2
    assert s.validate(first["token"], "laptop-1", now=1)["valid"] is False
    assert s.validate(second["token"], "tablet-2", now=1)["valid"] is False


def test_password_change_revokes_every_family_of_user():
    s = store()
    first = login(s, device="laptop-1")
    second = s.issue("alice", "tablet-2", now=0)
    other = s.issue("bob", "phone-3", now=0)
    result = s.change_password("alice")
    assert result["revoked_families"] == 2
    assert s.validate(first["token"], "laptop-1", now=1)["valid"] is False
    assert s.validate(second["token"], "tablet-2", now=1)["valid"] is False
    assert s.validate(other["token"], "phone-3", now=1)["valid"] is True


def test_sliding_expiry_extends_on_use():
    s = store(sliding_seconds=30 * DAY, max_lifetime_seconds=90 * DAY)
    issued = login(s)
    assert s.validate(issued["token"], "laptop-1", now=29 * DAY)["valid"] is True
    assert s.validate(issued["token"], "laptop-1", now=58 * DAY)["valid"] is True
    assert s.validate(issued["token"], "laptop-1", now=87 * DAY)["valid"] is True


def test_max_lifetime_caps_sliding_renewal():
    s = store(sliding_seconds=30 * DAY, max_lifetime_seconds=90 * DAY)
    issued = login(s)
    for day in (20, 40, 60, 80):
        assert s.validate(issued["token"], "laptop-1", now=day * DAY)["valid"] is True
    expired = s.validate(issued["token"], "laptop-1", now=90 * DAY)
    assert expired["valid"] is False
    assert expired["reason"] == "expired"


def test_idle_token_expires_after_sliding_window():
    s = store(sliding_seconds=30 * DAY)
    issued = login(s)
    expired = s.validate(issued["token"], "laptop-1", now=31 * DAY)
    assert expired["valid"] is False
    assert expired["reason"] == "expired"


def test_snapshot_never_contains_raw_credentials():
    s = store()
    issued = login(s)
    snapshot = s.snapshot()
    serialized = json.dumps(snapshot)
    assert issued["token"] not in serialized
    digests = snapshot["digests"]
    assert digests
    assert all(len(digest) == 64 for digest in digests)
    assert issued["token"] not in digests


def test_token_collision_is_resolved_without_overwriting_account():
    class Factory:
        def __init__(self):
            self.values = ["dup", "dup", "uniq"]
            self.index = 0

        def __call__(self):
            value = self.values[min(self.index, len(self.values) - 1)]
            self.index += 1
            return value

    s = store(token_factory=Factory())
    first = s.issue("alice", "laptop-1", now=0)
    second = s.issue("bob", "phone-2", now=0)
    assert first["token"] == "dup"
    assert second["token"] == "uniq"
    assert s.validate(first["token"], "laptop-1", now=1,
                      user="alice")["valid"] is True
    assert s.validate(second["token"], "phone-2", now=1,
                      user="bob")["valid"] is True
    assert s.validate(second["token"], "phone-2", now=1,
                      user="alice")["valid"] is False
