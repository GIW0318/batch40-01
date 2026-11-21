import pytest

import realm_merge_directory as rmd


def build():
    return rmd.RealmMergeDirectory(["s1", "s2"])


def two_characters(directory):
    directory.add_character("s1", "c1", "acct1", "Alice")
    directory.add_character("s1", "c2", "acct2", "Bob")


def test_name_conflict_keeps_higher_priority_server():
    directory = build()
    directory.add_character("s2", "c1", "acct1", "Hero")
    directory.add_character("s1", "c2", "acct2", "Hero")
    directory.merge()
    assert directory.resolve_name("c2") == "Hero"
    assert directory.resolve_name("c1") == "Hero@s2"


def test_rename_queue_lists_losing_entities():
    directory = build()
    directory.add_character("s2", "c1", "acct1", "Hero")
    directory.add_character("s1", "c2", "acct2", "Hero")
    directory.merge()
    assert directory.rename_queue() == [
        {
            "kind": "character",
            "entity_id": "c1",
            "old_name": "Hero",
            "new_name": "Hero@s2",
        }
    ]


def test_guild_name_conflict_uses_same_rule():
    directory = build()
    directory.add_guild("s2", "g1", "Legion")
    directory.add_guild("s1", "g2", "Legion")
    directory.merge()
    assert directory.resolve_name("g2") == "Legion"
    assert directory.resolve_name("g1") == "Legion@s2"


def test_mail_is_migrated_and_deduplicated():
    directory = build()
    two_characters(directory)
    directory.add_mail("s2", "m1", "c1", "old")
    directory.add_mail("s1", "m1", "c1", "new")
    directory.merge()
    directory.migrate_social()
    box = directory.mail_box("c1")
    assert len(box) == 1
    assert box[0]["subject"] == "new"


def test_listings_get_fresh_unique_ids():
    directory = build()
    two_characters(directory)
    directory.add_listing("s1", "x", "c1", "sword")
    directory.add_listing("s2", "x", "c2", "sword")
    directory.merge()
    directory.migrate_social()
    rows = directory.listings()
    assert [row["listing_id"] for row in rows] == ["L1", "L2"]
    assert {row["original_listing_id"] for row in rows} == {"x"}
    assert {row["seller_id"] for row in rows} == {"c1", "c2"}


def test_friend_links_are_rebuilt_bidirectionally():
    directory = build()
    two_characters(directory)
    directory.add_friend("s1", "c1", "c2")
    directory.merge()
    directory.migrate_social()
    assert directory.friends("c1") == ["c2"]
    assert directory.friends("c2") == ["c1"]


def test_blacklist_survives_merge():
    directory = build()
    two_characters(directory)
    directory.add_blacklist("s1", "c1", "c2")
    directory.merge()
    directory.migrate_social()
    assert directory.is_blocked("c1", "c2") is True
    assert directory.is_blocked("c2", "c1") is False


def test_leaderboard_deduplicates_by_account():
    directory = build()
    directory.add_character("s1", "c1", "acct", "A")
    directory.add_character("s2", "c2", "acct", "B")
    directory.add_character("s1", "c3", "other", "C")
    directory.report_score("s1", "acct", 10)
    directory.report_score("s2", "acct", 30)
    directory.report_score("s1", "other", 20)
    directory.merge()
    directory.migrate_social()
    board = directory.leaderboard()
    assert [row["account"] for row in board] == ["acct", "other"]
    assert [row["score"] for row in board] == [30, 20]
    assert [row["rank"] for row in board] == [1, 2]


def test_merge_batches_pause_and_resume():
    directory = build()
    two_characters(directory)
    first = directory.merge(batch_size=1)
    assert first == {"processed": 1, "remaining": 1, "complete": False}
    assert directory.resolve_name("c1") == "Alice"
    with pytest.raises(KeyError):
        directory.resolve_name("c2")
    second = directory.merge(batch_size=1)
    assert second["processed"] == 1
    assert second["complete"] is True
    assert directory.resolve_name("c2") == "Bob"


def test_rollback_undoes_only_latest_batch():
    directory = build()
    two_characters(directory)
    directory.merge(batch_size=1)
    directory.merge(batch_size=1)
    assert directory.rollback() == 1
    with pytest.raises(KeyError):
        directory.resolve_name("c2")
    assert directory.resolve_name("c1") == "Alice"


def test_rollback_keeps_entities_registered_after_batch():
    directory = build()
    directory.add_character("s1", "c1", "acct1", "Alice")
    directory.merge()
    directory.add_character("s1", "c2", "acct2", "Bob")
    assert directory.rollback() == 1
    with pytest.raises(KeyError):
        directory.resolve_name("c1")
    directory.merge()
    assert directory.resolve_name("c2") == "Bob"


def test_login_is_rejected_during_incomplete_merge():
    directory = build()
    two_characters(directory)
    directory.merge(batch_size=1)
    with pytest.raises(RuntimeError):
        directory.login("c1")


def test_login_returns_merged_name_after_completion():
    directory = build()
    directory.add_character("s1", "c1", "acct1", "Alice")
    directory.merge()
    assert directory.login("c1") == {"character_id": "c1", "name": "Alice"}
    with pytest.raises(KeyError):
        directory.login("missing")


def test_friend_display_name_resolves_after_rename():
    directory = build()
    directory.add_character("s2", "c1", "acct1", "Hero")
    directory.add_character("s1", "c2", "acct2", "Hero")
    directory.add_friend("s2", "c1", "c2")
    directory.merge()
    directory.migrate_social()
    assert directory.login("c1")["name"] == "Hero@s2"
    assert directory.friends("c1") == ["c2"]
    assert directory.resolve_name(directory.friends("c1")[0]) == "Hero"


def test_migrate_social_requires_complete_merge():
    directory = build()
    directory.add_character("s1", "c1", "acct1", "Alice")
    with pytest.raises(RuntimeError):
        directory.migrate_social()
