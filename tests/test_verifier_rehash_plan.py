import verifier_rehash_plan as vh

ARGON_TARGET = {"algo": "argon2id", "params": {"m": 1024, "t": 2, "p": 1},
                "version": 2}
BCRYPT_TARGET = {"algo": "bcrypt", "params": {"cost": 12}, "version": 2}
ARGON_PARAMS = {"m": 1024, "t": 2, "p": 1}


def plan(target=None, **kwargs):
    return vh.VerifierRehashPlan(target=target or ARGON_TARGET, **kwargs)


def legacy_argon(password, **kwargs):
    return vh.VerifierRehashPlan.hash_password(
        password, algo="argon2id", params={"m": 256, "t": 2, "p": 1},
        pepper_version=2, version=2, **kwargs
    )


def test_bcrypt_low_cost_is_rehashed_on_successful_login():
    p = plan(BCRYPT_TARGET)
    p.register("alice", vh.VerifierRehashPlan.hash_password(
        "pw", algo="bcrypt", params={"cost": 4}, pepper_version=2,
        version=2, salt="s1"))
    result = p.login("alice", "pw")
    assert result["ok"] is True
    assert result["rehashed"] is True
    assert result["pending"] == 1
    upgraded = p.parse(p.record("alice"))
    assert upgraded["algo"] == "bcrypt"
    assert upgraded["params"]["cost"] == 12
    assert upgraded["version"] == 2


def test_bcrypt_at_target_cost_is_not_rehashed():
    p = plan(BCRYPT_TARGET)
    p.register("alice", vh.VerifierRehashPlan.hash_password(
        "pw", algo="bcrypt", params={"cost": 12}, pepper_version=2,
        version=2, salt="s1"))
    result = p.login("alice", "pw")
    assert result["ok"] is True
    assert result["rehashed"] is False
    assert p.pending() == 0


def test_argon2_memory_is_compared_numerically():
    p = plan()
    p.register("alice", legacy_argon("pw"))
    result = p.login("alice", "pw")
    assert result["rehashed"] is True
    upgraded = p.parse(p.record("alice"))
    assert upgraded["params"]["m"] == 1024


def test_stronger_argon2_parameters_are_not_downgraded():
    p = plan()
    p.register("alice", vh.VerifierRehashPlan.hash_password(
        "pw", params={"m": 2048, "t": 3, "p": 1}, pepper_version=2,
        version=2, salt="strong"))
    result = p.login("alice", "pw")
    assert result["ok"] is True
    assert result["rehashed"] is False


def test_old_pepper_verifies_then_rewrites_with_current_pepper():
    p = plan()
    p.register("alice", vh.VerifierRehashPlan.hash_password(
        "pw", params=ARGON_PARAMS, pepper_version=1, version=2, salt="old"))
    result = p.login("alice", "pw")
    assert result["ok"] is True
    assert result["rehashed"] is True
    upgraded = p.parse(p.record("alice"))
    assert upgraded["pepper"] == 2


def test_unknown_pepper_version_fails_without_pepperless_fallback():
    p = plan()
    p.register("alice", vh.VerifierRehashPlan.hash_password(
        "pw", params=ARGON_PARAMS, pepper_version=7, version=2, salt="odd"))
    before = p.record("alice")
    result = p.login("alice", "pw")
    assert result["ok"] is False
    assert result["reason"] == "invalid credentials"
    assert p.record("alice") == before


def test_corrupt_record_goes_to_reset_without_aborting_batch():
    p = plan()
    p.register("alice", "not-a-valid-hash")
    result = p.login("alice", "pw")
    assert result["ok"] is False
    assert result["reason"] == "reset required"
    assert p.stats()["corrupt"] == 1
    p.register("bob", vh.VerifierRehashPlan.hash_password(
        "pw", params=ARGON_PARAMS, pepper_version=2, version=2, salt="bob"))
    assert p.login("bob", "pw")["ok"] is True


def test_unknown_user_runs_fake_verification_with_identical_response():
    p = plan()
    p.register("alice", vh.VerifierRehashPlan.hash_password(
        "right", params=ARGON_PARAMS, pepper_version=2, version=2, salt="a"))
    unknown = p.login("ghost", "pw")
    wrong = p.login("alice", "wrong")
    assert unknown == wrong
    assert unknown["reason"] == "invalid credentials"
    assert p.stats()["fake_verifications"] == 1


def test_concurrent_logins_rehash_only_once():
    p = plan()
    p.register("alice", legacy_argon("pw"))
    first = p.login("alice", "pw")
    second = p.login("alice", "pw")
    assert first["rehashed"] is True
    assert second["rehashed"] is False
    assert p.stats()["rehashes"] == 1
    assert p.pending() == 1


def test_upgrade_queue_deduplicates_per_user():
    p = plan()
    for name in ("alice", "bob"):
        p.register(name, legacy_argon("pw", salt=name))
        assert p.login(name, "pw")["rehashed"] is True
        assert p.login(name, "pw")["rehashed"] is False
    assert p.pending() == 2
    tasks = p.plan()
    assert sorted(task["user"] for task in tasks) == ["alice", "bob"]
    drained = p.drain()
    assert len(drained) == 2
    assert p.pending() == 0
    assert p.plan() == []


def test_rehash_salts_are_unique_per_user():
    p = plan()
    for name in ("alice", "bob"):
        p.register(name, legacy_argon("pw", salt=name))
        p.login(name, "pw")
    alice = p.parse(p.record("alice"))
    bob = p.parse(p.record("bob"))
    assert alice["salt"] != bob["salt"]
    assert len(alice["salt"]) >= 8
    assert len(bob["salt"]) >= 8


def test_low_record_version_is_upgraded():
    p = plan()
    p.register("alice", vh.VerifierRehashPlan.hash_password(
        "pw", params=ARGON_PARAMS, pepper_version=2, version=0, salt="v0"))
    result = p.login("alice", "pw")
    assert result["ok"] is True
    assert result["rehashed"] is True
    assert p.parse(p.record("alice"))["version"] == 2


def test_higher_record_version_is_rejected_not_accepted():
    p = plan()
    p.register("alice", vh.VerifierRehashPlan.hash_password(
        "pw", params=ARGON_PARAMS, pepper_version=2, version=9, salt="v9"))
    result = p.login("alice", "pw")
    assert result["ok"] is False
    assert result["reason"] == "unsupported version"
    assert result["rehashed"] is False


def test_overlong_password_is_rejected_before_hashing():
    p = plan()
    p.register("alice", vh.VerifierRehashPlan.hash_password(
        "pw", params=ARGON_PARAMS, pepper_version=2, version=2, salt="a"))
    before = p.record("alice")
    result = p.login("alice", "x" * 5000)
    assert result["ok"] is False
    assert result["reason"] == "password too long"
    assert p.record("alice") == before
    assert p.stats()["verifications"] == 0


def test_report_exposes_current_target_and_pending_counts():
    p = plan(BCRYPT_TARGET)
    for name in ("alice", "bob"):
        p.register(name, vh.VerifierRehashPlan.hash_password(
            "pw", algo="bcrypt", params={"cost": 4}, pepper_version=2,
            version=2, salt=name))
        p.login(name, "pw")
    report = p.report()
    assert report["current"] == report["target"]
    assert report["target"]["algo"] == "bcrypt"
    assert report["target"]["params"]["cost"] == 12
    assert report["pending"] == 2
