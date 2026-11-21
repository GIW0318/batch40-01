import battleground_credit_ledger as bcl


def ledger(window=8.0):
    book = bcl.BattlegroundCreditLedger(assist_window=window)
    for player_id, team in [
        ("tank", "red"),
        ("rogue", "red"),
        ("healer", "red"),
        ("mage", "blue"),
        ("victim", "blue"),
    ]:
        book.add_player(player_id, team)
    return book


def test_overkill_is_split_from_effective_damage():
    book = ledger()
    result = book.record_damage(1, "tank", "victim", 120, 50, 0.0)
    assert result["effective"] == 50
    assert result["overkill"] == 70
    assert result["credited_to"] == "tank"
    credited = book.credits_for_player("tank")
    assert len(credited) == 1
    assert credited[0]["amount"] == 50


def test_duplicate_event_sequence_is_settled_only_once():
    book = ledger()
    book.record_damage(7, "tank", "victim", 30, 100, 0.0)
    duplicate = book.record_damage(7, "rogue", "victim", 40, 70, 0.5)
    assert duplicate["duplicate"] is True
    assert duplicate["effective"] == 0
    assert len(book.credits_for_event(7)) == 1
    assert len(book.credits_for_player("rogue")) == 0


def test_killing_blow_gets_the_kill_and_top_damage_gets_assist():
    book = ledger()
    book.record_damage(1, "tank", "victim", 90, 100, 0.0)
    book.record_damage(2, "rogue", "victim", 10, 10, 1.0)
    kill = book.record_kill(3, "rogue", "victim", 1.0)
    assert kill["killer"] == "rogue"
    assert kill["assists"] == ["tank"]
    rows = {row["player_id"]: row for row in book.standings()}
    assert rows["rogue"]["kills"] == 1
    assert rows["tank"]["kills"] == 0
    assert rows["tank"]["assists"] == 1


def test_simultaneous_kill_reports_credit_the_victim_once():
    book = ledger()
    first = book.record_kill(10, "tank", "victim", 5.0)
    second = book.record_kill(11, "rogue", "victim", 5.0)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert sum(row["kills"] for row in book.standings()) == 1
    assert book.credits_for_player("rogue") == []


def test_pet_damage_and_pet_kill_are_credited_to_the_owner():
    book = ledger()
    book.add_player("pet", "red", pet_of="tank")
    damage = book.record_damage(1, "pet", "victim", 40, 100, 0.0)
    assert damage["credited_to"] == "tank"
    kill = book.record_kill(2, "pet", "victim", 1.0)
    assert kill["killer"] == "tank"
    assert kill["assists"] == []
    rows = {row["player_id"]: row for row in book.standings()}
    assert rows["tank"]["kills"] == 1
    assert "pet" not in rows


def test_assist_window_is_measured_against_the_kill_time():
    stale = ledger(8.0)
    stale.record_damage(1, "tank", "victim", 25, 100, 1.0)
    stale.record_kill(2, "rogue", "victim", 10.0)
    assert stale.assist("victim") == []

    fresh = ledger(8.0)
    fresh.record_damage(1, "tank", "victim", 25, 100, 5.0)
    fresh.record_kill(2, "rogue", "victim", 10.0)
    assert fresh.assist("victim") == ["tank"]


def test_only_effective_healing_counts_towards_an_assist():
    book = ledger()
    heal = book.record_heal(1, "healer", "tank", 100, 90, 100, 0.0)
    assert heal["effective"] == 10
    assert heal["overheal"] == 90
    book.record_kill(2, "mage", "tank", 1.0)
    assert book.assist("tank") == ["healer"]


def test_respawn_clears_assists_from_the_previous_life():
    book = ledger()
    book.record_damage(1, "tank", "victim", 20, 100, 1.0)
    book.respawn("victim", 2.0)
    book.record_kill(2, "rogue", "victim", 3.0)
    assert book.assist("victim") == []

    book.respawn("victim", 5.0)
    book.record_damage(3, "tank", "victim", 20, 100, 6.0)
    book.record_kill(4, "rogue", "victim", 7.0)
    assert book.assist("victim") == ["tank"]


def test_settle_freezes_the_standings():
    book = ledger()
    book.record_damage(1, "tank", "victim", 10, 100, 1.0)
    result = book.settle(winner_team="red")
    frozen = book.standings()
    late_damage = book.record_damage(2, "tank", "victim", 10, 90, 2.0)
    late_kill = book.record_kill(3, "tank", "victim", 2.0)
    assert result["frozen"] is True
    assert late_damage["ignored"] == "settled"
    assert late_kill["ignored"] == "settled"
    assert book.standings() == frozen


def test_repeated_settle_does_not_pay_honor_or_mail_twice():
    book = ledger()
    book.record_kill(1, "tank", "victim", 1.0)
    first = book.settle(winner_team="red")
    second = book.settle(winner_team="red")
    assert first == second
    tank_mails = [m for m in second["mails"] if m["player_id"] == "tank"]
    assert len(tank_mails) == 1
    assert tank_mails[0]["honor"] == 5


def test_leaving_truncates_damage_credit_at_the_leave_time():
    book = ledger()
    before = book.record_damage(1, "tank", "victim", 10, 100, 5.0)
    book.leave("tank", 10.0)
    at_leave = book.record_damage(2, "tank", "victim", 10, 90, 10.0)
    after = book.record_damage(3, "tank", "victim", 10, 80, 11.0)
    assert before["credited"] is True
    assert at_leave["credited"] is False
    assert after["credited"] is False
    assert len(book.credits_for_player("tank")) == 1


def test_kicked_player_receives_no_victory_honor_and_no_mail():
    book = ledger()
    book.record_kill(1, "tank", "victim", 1.0)
    book.leave("tank", 2.0, kicked=True)
    result = book.settle(winner_team="red")
    rows = {row["player_id"]: row for row in result["standings"]}
    assert rows["tank"]["honor"] == 3
    assert rows["rogue"]["honor"] == 2
    assert all(mail["player_id"] != "tank" for mail in result["mails"])


def test_each_player_gets_exactly_one_merged_mail():
    book = ledger()
    book.record_kill(1, "tank", "victim", 1.0)
    result = book.settle(winner_team="red")
    ids = [mail["player_id"] for mail in result["mails"]]
    assert len(ids) == 5
    assert len(ids) == len(set(ids))
    for mail in result["mails"]:
        assert mail["parts"] == 1
        assert {"kills", "assists", "honor"} <= set(mail)


def test_standings_are_returned_as_copies():
    book = ledger()
    book.record_damage(1, "tank", "victim", 10, 100, 1.0)
    rows = book.standings()
    rows[0]["honor"] = 999
    rows.append({"player_id": "intruder"})
    again = book.standings()
    assert all(row["player_id"] != "intruder" for row in again)
    assert all(row["honor"] != 999 for row in again)


def test_substitution_cuts_damage_attribution_from_the_old_player():
    book = ledger()
    book.record_damage(1, "tank", "victim", 10, 100, 1.0)
    book.replace_player("tank", "sub", 5.0)
    result = book.record_damage(2, "sub", "victim", 20, 90, 6.0)
    assert result["credited_to"] == "sub"
    old_credits = book.credits_for_player("tank")
    assert [row["event_seq"] for row in old_credits] == [1]
    event_credits = book.credits_for_event(2)
    assert len(event_credits) == 1
    assert event_credits[0]["credited_to"] == "sub"
