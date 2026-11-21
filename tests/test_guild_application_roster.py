import pytest

import guild_application_roster as gar


def make_roster(capacity=4, ttl=100.0):
    roster = gar.GuildApplicationRoster(application_ttl=ttl)
    roster.add_guild("g1", "gm", capacity=capacity, officers=["officer"],
                     members=["gm", "officer", "veteran"])
    for index in range(1, 6):
        roster.add_account("acct%d" % index, ["char%d" % index])
    return roster


def test_duplicate_pending_application_updates_existing_record():
    roster = make_roster()
    first = roster.apply("g1", "char1", account_id="acct1", note="first", now=1.0)
    second = roster.apply("g1", "char1", account_id="acct1", note="second", now=2.0)
    assert second["application_id"] == first["application_id"]
    assert second["duplicate"] is True
    applications = roster.applications("g1")
    assert len(applications) == 1
    assert applications[0]["note"] == "second"


def test_expired_application_is_rejected_and_marked_expired():
    roster = make_roster(ttl=100.0)
    application = roster.apply("g1", "char1", account_id="acct1", now=0.0)
    result = roster.approve("g1", application["application_id"], "officer", now=101.0)
    assert result["approved"] is False
    assert result["reason"] == "expired"
    assert roster.applications("g1")[0]["status"] == "expired"
    assert "char1" not in roster.members("g1")


def test_full_guild_rejects_approval_without_adding_member():
    roster = make_roster(capacity=3)
    application = roster.apply("g1", "char1", account_id="acct1", now=0.0)
    result = roster.approve("g1", application["application_id"], "officer", now=1.0)
    assert result["approved"] is False
    assert result["reason"] == "full"
    assert len(roster.members("g1")) == 3


def test_applicant_already_in_another_guild_cannot_be_approved():
    roster = make_roster()
    roster.add_account("acct3", ["char3"], guild="g2")
    application = roster.apply("g1", "char3", account_id="acct3", now=0.0)
    result = roster.approve("g1", application["application_id"], "officer", now=1.0)
    assert result["approved"] is False
    assert result["reason"] == "already_in_guild"
    assert "char3" not in roster.members("g1")


def test_rejected_application_cannot_be_reused():
    roster = make_roster()
    application = roster.apply("g1", "char1", account_id="acct1", now=0.0)
    roster.reject("g1", application["application_id"], "officer", reason="no", now=1.0)
    result = roster.approve("g1", application["application_id"], "officer", now=2.0)
    assert result["approved"] is False
    assert result["reason"] == "not_pending"
    assert "char1" not in roster.members("g1")


def test_approval_authority_follows_leadership_transfer():
    roster = make_roster()
    application = roster.apply("g1", "char1", account_id="acct1", now=0.0)
    roster.transfer_leadership("g1", "officer")
    denied = roster.approve("g1", application["application_id"], "gm", now=1.0)
    assert denied["approved"] is False
    assert denied["reason"] == "not_authorized"
    allowed = roster.approve("g1", application["application_id"], "officer", now=2.0)
    assert allowed["approved"] is True
    assert roster.events_for("g1")[-1]["actor"] == "officer"


def test_blacklist_is_keyed_by_account_not_character_name():
    roster = make_roster()
    roster.set_blacklist("acct4", True)
    result = roster.apply("g1", "renamed-character", account_id="acct4", now=0.0)
    assert result["ok"] is False
    assert result["reason"] == "blacklisted"
    assert roster.applications("g1") == []


def test_batch_approval_returns_per_item_results_and_only_successes_join():
    roster = make_roster(capacity=5)
    ids = []
    for index in range(1, 5):
        ids.append(roster.apply("g1", "char%d" % index, account_id="acct%d" % index,
                                now=float(index))["application_id"])
    results = roster.approve_batch("g1", ids, "officer", now=10.0)
    assert len(results) == 4
    assert sum(1 for result in results if result["approved"]) == 2
    assert len(roster.members("g1")) == 5
    assert sum(1 for result in results if result["reason"] == "full") == 2


def test_application_list_is_newest_first():
    roster = make_roster()
    first = roster.apply("g1", "char1", account_id="acct1", now=10.0)
    second = roster.apply("g1", "char2", account_id="acct2", now=30.0)
    third = roster.apply("g1", "char3", account_id="acct3", now=20.0)
    order = [entry["application_id"] for entry in roster.applications("g1")]
    assert order == [second["application_id"], third["application_id"],
                     first["application_id"]]


def test_multiline_note_stays_one_application():
    roster = make_roster()
    note = "first line\nsecond line"
    roster.apply("g1", "char1", account_id="acct1", note=note, now=0.0)
    applications = roster.applications("g1")
    assert len(applications) == 1
    assert applications[0]["note"] == note


def test_batch_approval_processes_oldest_first_and_stops_at_capacity():
    roster = make_roster(capacity=5)
    newest = roster.apply("g1", "char1", account_id="acct1", now=30.0)
    oldest = roster.apply("g1", "char2", account_id="acct2", now=10.0)
    middle = roster.apply("g1", "char3", account_id="acct3", now=20.0)
    results = roster.approve_batch(
        "g1",
        [newest["application_id"], oldest["application_id"], middle["application_id"]],
        "officer",
        now=40.0,
    )
    approved = [result["application_id"] for result in results if result["approved"]]
    assert approved == [oldest["application_id"], middle["application_id"]]
    blocked = [result for result in results
               if result["application_id"] == newest["application_id"]][0]
    assert blocked["reason"] == "full"


def test_duplicate_approval_lands_only_one_result():
    roster = make_roster()
    application = roster.apply("g1", "char1", account_id="acct1", now=0.0)
    first = roster.approve("g1", application["application_id"], "officer", now=1.0)
    second = roster.approve("g1", application["application_id"], "officer", now=2.0)
    assert first["approved"] is True
    assert second["approved"] is False
    assert second["reason"] == "not_pending"
    assert roster.members("g1").count("char1") == 1
    approval_events = [event for event in roster.events_for("g1")
                       if event["application_id"] == application["application_id"]
                       and event["new_state"] == "approved"]
    assert len(approval_events) == 1


def test_capacity_report_separates_pending_from_members():
    roster = make_roster()
    roster.apply("g1", "char1", account_id="acct1", now=1.0)
    roster.apply("g1", "char2", account_id="acct2", now=2.0)
    report = roster.capacity("g1")
    assert report["limit"] == 4
    assert report["members"] == 3
    assert report["pending"] == 2
    assert report["available"] == 1


def test_expansion_allows_previously_full_guild_to_approve():
    roster = make_roster(capacity=3)
    application = roster.apply("g1", "char1", account_id="acct1", now=0.0)
    first = roster.approve("g1", application["application_id"], "officer", now=1.0)
    assert first["reason"] == "full"
    roster.expand("g1", 4)
    second = roster.approve("g1", application["application_id"], "officer", now=2.0)
    assert second["approved"] is True
    assert len(roster.members("g1")) == 4


def test_approval_event_is_pushed_to_all_managers_with_state_change():
    roster = make_roster()
    application = roster.apply("g1", "char1", account_id="acct1", now=0.0)
    roster.approve("g1", application["application_id"], "officer", now=1.0)
    event = roster.events_for("g1")[-1]
    assert event["application_id"] == application["application_id"]
    assert event["old_state"] == "pending"
    assert event["new_state"] == "approved"
    assert event["actor"] == "officer"
    assert "gm" in event["audience"]
    assert "officer" in event["audience"]
