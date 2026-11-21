import pvp_forfeit_arbiter as pfa


def arbiter(**kwargs):
    kwargs.setdefault("vote_threshold", 0.5)
    kwargs.setdefault("grace", 30.0)
    kwargs.setdefault("cooldown", 5.0)
    return pfa.PvpForfeitArbiter(**kwargs)


def make(book, match_id="m1", size=3):
    teams = {
        "A": ["a%d" % index for index in range(size)],
        "B": ["b%d" % index for index in range(size)],
    }
    book.create_match(match_id, teams)
    return teams


def test_majority_vote_of_a_team_passes():
    book = arbiter()
    make(book)
    first = book.vote_forfeit("m1", "A", "a0", now=0.0)
    second = book.vote_forfeit("m1", "A", "a1", now=1.0)
    assert first["status"] == "counted"
    assert first["required"] == 2
    assert second["status"] == "passed"
    assert second["winner"] == "B"


def test_disconnected_member_abstains_from_the_denominator():
    book = arbiter()
    make(book, size=4)
    book.disconnect("m1", "a3", now=0.0)
    book.vote_forfeit("m1", "A", "a0", now=1.0)
    result = book.vote_forfeit("m1", "A", "a1", now=2.0)
    assert result["status"] == "passed"
    assert result["required"] == 2


def test_one_disconnect_does_not_immediately_lose_the_match():
    book = arbiter()
    make(book)
    book.disconnect("m1", "a0", now=0.0)
    assert book.tick(10.0)["finalized"] == []
    assert book.record("m1")["status"] == "active"
    assert book.record("m1")["winner"] is None


def test_reconnect_within_grace_restores_the_player():
    book = arbiter()
    make(book)
    book.disconnect("m1", "a0", now=0.0)
    assert book.reconnect("m1", "a0", now=20.0)["status"] == "restored"
    assert book.tick(29.0)["finalized"] == []
    assert book.tick(40.0)["finalized"] == []


def test_disconnect_timeout_finalizes_a_loss():
    book = arbiter()
    make(book)
    book.disconnect("m1", "a0", now=0.0)
    result = book.tick(30.0)
    record = book.record("m1")
    assert result["finalized"] == ["m1"]
    assert record["status"] == "finished"
    assert record["winner"] == "B"
    assert record["reason"] == "disconnect"


def test_finalize_can_only_succeed_once():
    book = arbiter()
    make(book)
    first = book.finalize("m1", winner="A", now=1.0)
    second = book.finalize("m1", winner="B", now=2.0)
    assert first["finalized"] is True
    assert second["finalized"] is False
    assert second["reason"] == "already finished"
    assert book.vote_forfeit("m1", "B", "b0", now=3.0)["status"] == "finished"
    assert book.disconnect("m1", "a0", now=4.0)["status"] == "finished"
    assert book.reconnect("m1", "a0", now=5.0)["status"] == "finished"


def test_late_forfeit_vote_cannot_change_the_result():
    book = arbiter()
    make(book)
    book.finalize("m1", winner="A", now=1.0)
    result = book.vote_forfeit("m1", "B", "b0", now=2.0)
    assert result["status"] == "finished"
    assert book.record("m1")["winner"] == "A"


def test_forfeiting_team_receives_no_season_points():
    book = arbiter()
    make(book)
    book.vote_forfeit("m1", "B", "b0", now=0.0)
    book.vote_forfeit("m1", "B", "b1", now=1.0)
    record = book.record("m1")
    assert record["winner"] == "A"
    assert record["points"] == {"A": 3, "B": 0}


def test_draw_is_not_decided_by_vote_counts():
    book = arbiter()
    make(book)
    result = book.finalize("m1", winner=None, reason="draw", now=1.0)
    assert result["finalized"] is True
    book.vote_forfeit("m1", "A", "a0", now=2.0)
    record = book.record("m1")
    assert record["winner"] is None
    assert record["points"] == {"A": 0, "B": 0}


def test_forfeit_vote_cooldown_is_per_player_and_match():
    book = arbiter(cooldown=5.0)
    make(book)
    assert book.vote_forfeit("m1", "A", "a0", now=0.0)["status"] == "counted"
    blocked = book.vote_forfeit("m1", "A", "a0", now=2.0)
    assert blocked["status"] == "cooldown"
    assert blocked["retry_at"] == 5.0
    assert book.vote_forfeit("m1", "A", "a0", now=5.0)["status"] == "counted"


def test_rematch_is_scheduled_once_after_the_result_is_written():
    book = arbiter()
    make(book)
    assert book.request_rematch("m1", now=0.0)["status"] == "not_finished"
    book.finalize("m1", winner="A", now=1.0)
    assert book.request_rematch("m1", now=2.0)["status"] == "scheduled"
    assert book.request_rematch("m1", now=3.0)["status"] == "already_requested"
    record = book.record("m1")
    assert record["rematch_scheduled"] is True
    assert [event["kind"] for event in record["events"]].count("rematch") == 1


def test_end_reason_follows_the_last_valid_event():
    book = arbiter()
    make(book, "m1")
    book.vote_forfeit("m1", "A", "a0", now=0.0)
    book.vote_forfeit("m1", "A", "a1", now=1.0)
    make(book, "m2")
    book.disconnect("m2", "b0", now=0.0)
    book.tick(30.0)
    assert book.record("m1")["reason"] == "forfeit"
    assert book.record("m2")["reason"] == "disconnect"


def test_all_events_share_one_increasing_sequence():
    book = arbiter()
    make(book)
    book.vote_forfeit("m1", "A", "a0", now=0.0)
    book.disconnect("m1", "a2", now=1.0)
    book.reconnect("m1", "a2", now=2.0)
    book.finalize("m1", winner="A", now=3.0)
    book.request_rematch("m1", now=4.0)
    events = book.record("m1")["events"]
    seqs = [event["seq"] for event in events]
    assert len(events) == 5
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))


def test_replay_reads_the_same_settlement_record():
    book = arbiter()
    make(book)
    book.disconnect("m1", "a1", now=0.0)
    book.tick(30.0)
    record = book.record("m1")
    replay = book.replay("m1")
    assert replay == {
        "winner": record["winner"],
        "reason": record["reason"],
        "points": record["points"],
        "teams": record["teams"],
    }


def test_both_teams_forfeiting_settles_only_the_first_result():
    book = arbiter()
    make(book)
    book.vote_forfeit("m1", "A", "a0", now=0.0)
    first = book.vote_forfeit("m1", "A", "a1", now=1.0)
    second = book.vote_forfeit("m1", "B", "b0", now=2.0)
    third = book.vote_forfeit("m1", "B", "b1", now=3.0)
    record = book.record("m1")
    assert first["status"] == "passed"
    assert second["status"] == "finished"
    assert third["status"] == "finished"
    assert record["winner"] == "B"
    assert record["points"] == {"A": 0, "B": 3}
    assert [event["kind"] for event in record["events"]].count("finalize") == 1
