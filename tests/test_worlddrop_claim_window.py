import pytest

import worlddrop_claim_window as wcw


def make_window():
    window = wcw.WorlddropClaimWindow(protection=60.0)
    window.add_player("tank", "A")
    window.add_player("healer", "A")
    window.add_player("ally", "A")
    window.add_player("enemy", "B")
    kill = window.register_kill(
        kill_id="k1",
        mob_id="elite",
        damage={"p1": 700, "p2": 300},
        last_hit_party="p2",
        party_members={"p1": ["tank", "healer"], "p2": ["rogue"]},
        factions={"p1": "A", "p2": "B"},
        map_id="m1",
        drops=[
            {"drop_id": "d1", "item_id": "sword", "quantity": 1},
            {"drop_id": "d2", "item_id": "gem", "quantity": 2},
        ],
        now=0.0,
    )
    return window, kill


def test_highest_effective_damage_wins_even_without_last_hit():
    window, kill = make_window()
    assert kill["owner_party"] == "p1"
    assert window.rights("d1", "tank")["allowed"] is True
    denied = window.rights("d1", "rogue")
    assert denied["allowed"] is False
    assert denied["reason"] == "faction"


def test_protection_blocks_same_faction_players_outside_the_party():
    window, _ = make_window()
    result = window.rights("d1", "ally")
    assert result["allowed"] is False
    assert result["reason"] == "protected"
    assert result["owner_party"] == "p1"


def test_cross_faction_player_is_denied_during_protection():
    window, _ = make_window()
    result = window.rights("d1", "enemy")
    assert result["allowed"] is False
    assert result["reason"] == "faction"


def test_client_clock_cannot_extend_or_shorten_protection():
    window, _ = make_window()
    early = window.claim("d1", "tank", now=59.0, client_now=99999.0)
    assert early["claimed"] is True
    late = window.claim("d2", "tank", now=60.0, client_now=-99999.0)
    assert late["claimed"] is False
    assert late["reason"] == "expired"


def test_party_leader_leaving_keeps_rights_for_remaining_member():
    window, _ = make_window()
    window.party_leave("p1", "tank", now=5.0)
    assert window.rights("d1", "healer")["allowed"] is True
    left = window.rights("d1", "tank")
    assert left["allowed"] is False
    assert left["reason"] == "protected"


def test_ownership_is_released_when_entire_party_leaves_the_map():
    window, _ = make_window()
    window.teleport("tank", "m2", now=5.0)
    window.teleport("healer", "m2", now=6.0)
    result = window.rights("d1", "ally")
    assert result["allowed"] is True
    assert result["owner_party"] is None


def test_same_drop_can_only_be_claimed_once():
    window, _ = make_window()
    first = window.claim("d1", "tank", now=1.0, request_id="pick-1")
    second = window.claim("d1", "tank", now=2.0, request_id="pick-1")
    third = window.claim("d1", "healer", now=3.0)
    assert first["claimed"] is True
    assert first["quest_count"] == 1
    assert second["duplicate"] is True
    assert second["quest_count"] == 1
    assert third["claimed"] is False
    assert third["reason"] == "already_claimed"
    assert third["quest_count"] == 1


def test_expire_mails_unclaimed_items_to_owner_and_expires_drop():
    window, _ = make_window()
    result = window.expire(now=60.0)
    assert sorted(result["expired"]) == ["d1", "d2"]
    queued = window.mail_queue()
    assert len(queued) == 2
    assert {entry["to"] for entry in queued} == {"tank"}
    assert window.rights("d1", "tank", now=61.0)["reason"] == "expired"


def test_corpse_and_drop_expire_at_the_same_moment():
    window, kill = make_window()
    corpse_id = kill["corpse_id"]
    assert window.corpse_state(corpse_id, now=59.0)["exists"] is True
    window.expire(now=60.0)
    assert window.corpse_state(corpse_id, now=60.0)["exists"] is False
    assert window.rights("d1", "tank", now=60.0)["reason"] == "expired"


def test_unreachable_drop_cannot_be_claimed_but_is_mailed_on_expiry():
    window, _ = make_window()
    window.register_kill(
        kill_id="k2",
        mob_id="elite",
        damage={"p1": 900, "p2": 100},
        last_hit_party="p1",
        party_members={"p1": ["tank", "healer"], "p2": ["rogue"]},
        factions={"p1": "A", "p2": "B"},
        map_id="m1",
        drops=[{"drop_id": "d3", "item_id": "ore", "quantity": 1,
                "reachable": False}],
        now=0.0,
    )
    denied = window.claim("d3", "tank", now=10.0)
    assert denied["claimed"] is False
    assert denied["reason"] == "unreachable"
    result = window.expire(now=60.0)
    mailed = [entry for entry in result["mailed"] if entry["drop_id"] == "d3"]
    assert len(mailed) == 1
    assert mailed[0]["item_id"] == "ore"


def test_snapshot_restore_preserves_remaining_protection():
    window, _ = make_window()
    snapshot = window.snapshot(now=20.0)
    restored = wcw.WorlddropClaimWindow(protection=60.0)
    restored.restore(snapshot, now=40.0)
    assert restored.rights("d1", "tank", now=79.0)["allowed"] is True
    assert restored.rights("d1", "tank", now=80.0)["reason"] == "expired"


def test_claimed_state_survives_restart():
    window, _ = make_window()
    window.claim("d1", "tank", now=10.0)
    snapshot = window.snapshot(now=20.0)
    restored = wcw.WorlddropClaimWindow(protection=60.0)
    restored.restore(snapshot, now=40.0)
    again = restored.claim("d1", "tank", now=41.0)
    assert again["claimed"] is False
    assert again["reason"] == "already_claimed"
    assert again["quest_count"] == 1


def test_teleporting_one_member_keeps_party_ownership():
    window, _ = make_window()
    window.teleport("tank", "m2", now=5.0)
    off_map = window.rights("d1", "tank")
    assert off_map["allowed"] is False
    assert off_map["reason"] == "off_map"
    held = window.rights("d1", "healer")
    assert held["allowed"] is True
    assert held["owner_party"] == "p1"


def test_exact_expiry_boundary_rejects_the_claim():
    window, _ = make_window()
    assert window.rights("d1", "tank", now=59.999)["allowed"] is True
    assert window.rights("d1", "tank", now=60.0)["reason"] == "expired"


def test_rights_report_owner_party_and_expiry_for_party_member():
    window, _ = make_window()
    result = window.rights("d1", "tank", now=1.0)
    assert result["allowed"] is True
    assert result["owner_party"] == "p1"
    assert result["expires_at"] == 60.0
    assert result["drop_id"] == "d1"
