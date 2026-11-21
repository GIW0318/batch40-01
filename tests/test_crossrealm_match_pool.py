import crossrealm_match_pool as cmp


ROLES = ("tank", "healer", "dps")


def party(prefix, rating=1500, roles=ROLES, latency=None):
    players = []
    for index, role in enumerate(roles):
        player = {
            "id": "%s%d" % (prefix, index),
            "role": role,
            "rating": rating,
            "latency": list(latency or [10, 20, 30]),
        }
        players.append(player)
    return players


def pool(**kwargs):
    kwargs.setdefault("team_size", 3)
    kwargs.setdefault("rating_band", 100)
    kwargs.setdefault("offline_timeout", 30.0)
    return cmp.CrossRealmMatchPool(**kwargs)


def test_premade_party_is_never_split_between_teams():
    book = pool()
    book.enqueue("A", party("a"), "r1", now=0.0)
    book.enqueue("B", party("b"), "r1", now=0.0)
    match = book.form_match(now=1.0)
    assert set(match["teams"][0]) == {"a0", "a1", "a2"}
    assert set(match["teams"][1]) == {"b0", "b1", "b2"}


def test_match_rating_is_the_party_average_not_the_leader():
    book = pool()
    first = [
        {"id": "a0", "role": "tank", "rating": 2000},
        {"id": "a1", "role": "healer", "rating": 1000},
        {"id": "a2", "role": "dps", "rating": 1000},
    ]
    second = [
        {"id": "b0", "role": "tank", "rating": 2000},
        {"id": "b1", "role": "healer", "rating": 1000},
        {"id": "b2", "role": "dps", "rating": 1000},
    ]
    book.enqueue("A", first, "r1", now=0.0)
    book.enqueue("B", second, "r1", now=0.0)
    match = book.form_match(now=1.0)
    assert abs(match["ratings"][0] - 4000.0 / 3.0) < 0.01
    assert match["ratings"][0] != 2000


def test_role_requirements_block_a_match():
    book = pool()
    book.enqueue("A", party("a", roles=("dps", "dps", "dps")), "r1", now=0.0)
    book.enqueue("B", party("b"), "r1", now=0.0)
    assert book.form_match(now=1.0) is None


def test_team_size_must_be_exact():
    book = pool()
    book.enqueue("A", party("a", roles=("tank", "healer")), "r1", now=0.0)
    book.enqueue("B", party("b"), "r1", now=0.0)
    assert book.form_match(now=1.0) is None


def test_cancel_after_match_cannot_remove_a_finished_roster():
    book = pool()
    book.enqueue("A", party("a"), "r1", now=0.0)
    book.enqueue("B", party("b"), "r1", now=0.0)
    match = book.form_match(now=1.0)
    result = book.dequeue("A")
    assert result["status"] == "matched"
    assert result["match_id"] == match["match_id"]
    assert "a0" in book.match_record(match["match_id"])["teams"][0]


def test_cancel_before_match_removes_the_party():
    book = pool()
    book.enqueue("A", party("a"), "r1", now=0.0)
    book.enqueue("B", party("b"), "r1", now=0.0)
    assert book.dequeue("A")["status"] == "cancelled"
    assert book.form_match(now=1.0) is None
    assert book.status()["queued"] == 1


def test_backfill_is_refused_for_an_ended_match():
    book = pool()
    book.enqueue("A", party("a"), "r1", now=0.0)
    book.enqueue("B", party("b"), "r1", now=0.0)
    match = book.form_match(now=1.0)
    book.end_match(match["match_id"], winner="A")
    result = book.backfill(
        match["match_id"], {"id": "x0", "role": "dps", "rating": 1500}, "dps", now=2.0
    )
    assert result["status"] == "ended"


def test_backfill_respects_the_role_gap():
    book = pool()
    book.enqueue("A", party("a"), "r1", now=0.0)
    book.enqueue("B", party("b"), "r1", now=0.0)
    match = book.form_match(now=1.0)
    for player_id in ("a0", "a1", "b0", "b1", "b2"):
        book.ping(player_id, 20.0)
    book.tick(32.0)
    result = book.backfill(
        match["match_id"], {"id": "x0", "role": "tank", "rating": 1500}, "tank", now=33.0
    )
    assert result["status"] == "role_full"


def test_duplicate_enqueue_for_the_same_party_is_rejected():
    book = pool()
    book.enqueue("A", party("a"), "r1", now=0.0)
    again = book.enqueue("A", party("a"), "r1", now=1.0)
    assert again["status"] == "duplicate"
    assert book.status()["queued"] == 1


def test_offline_timeout_releases_the_queue_slot():
    book = pool()
    book.enqueue("A", party("a"), "r1", now=0.0)
    result = book.tick(31.0)
    assert result["expired_parties"] == ["A"]
    assert book.status()["queued"] == 0
    assert book.form_match(now=32.0) is None


def test_longest_waiting_pair_wins_inside_the_rating_band():
    book = pool(rating_band=10)
    book.enqueue("A", party("a", rating=1500), "r1", now=0.0)
    book.enqueue("B", party("b", rating=1500), "r1", now=1.0)
    book.enqueue("C", party("c", rating=1505), "r1", now=2.0)
    match = book.form_match(now=3.0)
    assert match["party_ids"] == ["A", "B"]


def test_latency_uses_the_median_of_the_last_three_probes():
    book = pool()
    book.enqueue("A", party("a", latency=[200, 10, 20]), "r1", now=0.0)
    assert book.latency_for("a0") == 20
    book.probe("a0", 100)
    assert book.latency_for("a0") == 20
    book.probe("a0", 400)
    assert book.latency_for("a0") == 100


def test_a_player_cannot_queue_in_two_regions():
    book = pool()
    first = party("a")
    book.enqueue("A", first, "r1", now=0.0)
    second = [first[0], {"id": "d1", "role": "healer", "rating": 1500},
              {"id": "d2", "role": "dps", "rating": 1500}]
    result = book.enqueue("D", second, "r2", now=1.0)
    assert result["status"] == "duplicate"
    assert result["player_id"] == "a0"
    assert book.status()["queued"] == 1


def test_match_record_is_immutable_from_the_outside():
    book = pool()
    book.enqueue("A", party("a"), "r1", now=0.0)
    book.enqueue("B", party("b"), "r1", now=0.0)
    match = book.form_match(now=1.0)
    record = book.match_record(match["match_id"])
    record["teams"][0].append("intruder")
    record["status"] = "ended"
    again = book.match_record(match["match_id"])
    assert "intruder" not in again["teams"][0]
    assert again["status"] == "active"


def test_offline_player_in_a_match_frees_the_slot_for_backfill():
    book = pool()
    book.enqueue("A", party("a"), "r1", now=0.0)
    book.enqueue("B", party("b"), "r1", now=0.0)
    match = book.form_match(now=1.0)
    for player_id in ("a0", "a1", "b0", "b1", "b2"):
        book.ping(player_id, 20.0)
    dropped = book.tick(32.0)
    assert dropped["dropped_players"] == ["a2"]
    result = book.backfill(
        match["match_id"], {"id": "x0", "role": "dps", "rating": 1500}, "dps", now=33.0
    )
    assert result["status"] == "added"
    assert result["open_slots"] == 0
    record = book.match_record(match["match_id"])
    assert "x0" in record["teams"][0]
    assert "a2" not in record["teams"][0]
