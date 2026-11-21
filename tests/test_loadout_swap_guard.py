import loadout_swap_guard as lsg


def item(item_id, slot="armor", attack=0, **extra):
    data = {
        "item_id": item_id,
        "slot": slot,
        "attack": attack,
        "owner": "c1",
    }
    data.update(extra)
    return data


def base_character(guard, character_id="c1"):
    entries = [
        item("a", slot="weapon", attack=10, set="t1"),
        item("b", slot="armor", attack=10, set="t1"),
        item("c", slot="weapon", attack=7),
        item("d", slot="armor", attack=8),
    ]
    for entry in entries:
        entry["owner"] = character_id
    guard.register_character(
        character_id,
        items=entries,
        equipped=["a", "b"],
        bag=["c", "d"],
        bag_capacity=2,
        hotbar={"1": "old"},
        skills=["old"],
    )
    return guard


def test_swap_is_refused_when_the_bag_has_no_room_for_the_diff():
    guard = base_character(lsg.LoadoutSwapGuard())
    assert guard.save("c1", "only-c", ["c"])["saved"] is True
    before = guard.state("c1")
    result = guard.apply("c1", "only-c")
    assert result["applied"] is False
    assert result["reason"] == "not enough bag slots"
    assert guard.state("c1") == before


def test_swap_reserves_exactly_the_diff_that_must_return_to_the_bag():
    guard = base_character(lsg.LoadoutSwapGuard())
    guard.save("c1", "light", ["a", "d"])
    preview = guard.preview("c1", "light")
    assert preview["bag_slots_needed"] == 0
    result = guard.apply("c1", "light")
    assert result["applied"] is True
    assert result["added"] == ["d"]
    assert result["removed"] == ["b"]
    state = guard.state("c1")
    assert set(state["equipped"]) == {"a", "d"}
    assert set(state["bag"]) == {"b", "c"}


def test_one_item_cannot_belong_to_two_loadouts():
    guard = base_character(lsg.LoadoutSwapGuard())
    assert guard.save("c1", "one", ["a", "b"])["saved"] is True
    second = guard.save("c1", "two", ["b", "c"])
    assert second["saved"] is False
    assert "item already in loadout" in second["reason"]


def test_failed_transaction_rolls_back_equipment_and_bag():
    guard = base_character(lsg.LoadoutSwapGuard())
    guard.save("c1", "light", ["a", "d"])
    before = guard.state("c1")
    result = guard.apply("c1", "light", context={"fail_after": True})
    assert result["applied"] is False
    assert result["reason"] == "transaction_failed"
    assert guard.state("c1") == before


def test_two_handed_weapon_and_offhand_are_mutually_exclusive():
    guard = lsg.LoadoutSwapGuard()
    guard.register_character(
        "c2",
        items=[
            item("twoh", slot="weapon", attack=20, two_handed=True, owner="c2"),
            item("shield", slot="offhand", attack=5, owner="c2"),
        ],
        equipped=[],
        bag=["twoh", "shield"],
        bag_capacity=4,
    )
    result = guard.save("c2", "bad", ["twoh", "shield"])
    assert result["saved"] is False
    assert result["reason"] == "two-handed with offhand"


def test_set_bonus_is_recomputed_from_the_new_set():
    guard = lsg.LoadoutSwapGuard()
    guard.register_character(
        "c3",
        items=[
            item("o1", attack=10, set="t1", owner="c3"),
            item("o2", attack=10, set="t1", owner="c3"),
            item("n1", attack=5, set="t2", owner="c3"),
            item("n2", attack=5, set="t2", owner="c3"),
        ],
        equipped=["o1", "o2"],
        bag=["n1", "n2"],
        bag_capacity=2,
    )
    assert guard.stats("c3")["total"] == 30
    guard.save("c3", "new", ["n1", "n2"])
    result = guard.apply("c3", "new")
    assert result["stats"] == {"attack": 10, "set_bonus": 10, "total": 20}
    assert guard.stats("c3")["set_bonus"] == 10


def test_applying_the_same_loadout_twice_does_not_stack_stats():
    guard = base_character(lsg.LoadoutSwapGuard())
    guard.save("c1", "light", ["a", "d"])
    first = guard.apply("c1", "light")
    second = guard.apply("c1", "light")
    assert first["changed"] is True
    assert second["changed"] is False
    assert first["stats"] == second["stats"]
    assert guard.stats("c1")["total"] == 18


def test_combat_blocks_the_swap():
    guard = base_character(lsg.LoadoutSwapGuard())
    guard.save("c1", "light", ["a", "d"])
    result = guard.apply("c1", "light", context={"in_combat": True})
    assert result["applied"] is False
    assert result["reason"] == "in_combat"
    assert set(guard.state("c1")["equipped"]) == {"a", "b"}


def test_cooldown_blocks_the_swap():
    guard = base_character(lsg.LoadoutSwapGuard())
    guard.save("c1", "light", ["a", "d"])
    result = guard.apply(
        "c1", "light", context={"now": 5.0, "cooldown_until": 20.0}
    )
    assert result["applied"] is False
    assert result["reason"] == "cooldown"


def test_deleted_item_is_reported_at_preview_time():
    guard = base_character(lsg.LoadoutSwapGuard())
    guard.save("c1", "light", ["a", "d"])
    guard.delete_item("c1", "d")
    preview = guard.preview("c1", "light")
    assert preview["valid"] is False
    assert "deleted:d" in preview["errors"]
    result = guard.apply("c1", "light")
    assert result["applied"] is False
    assert "deleted:d" in result["errors"]


def test_loadout_cannot_use_another_characters_item():
    guard = lsg.LoadoutSwapGuard()
    guard.register_character(
        "c1",
        items=[item("a", slot="weapon", attack=10, owner="c1")],
        equipped=["a"],
        bag=[],
        bag_capacity=4,
    )
    guard.register_character(
        "c2",
        items=[item("a", slot="weapon", attack=10, owner="c1")],
        equipped=[],
        bag=["a"],
        bag_capacity=4,
    )
    result = guard.save("c2", "steal", ["a"])
    assert result["saved"] is False
    assert result["reason"] == "not owned:a"


def test_hotbar_and_skills_follow_the_loadout():
    guard = base_character(lsg.LoadoutSwapGuard())
    guard.save(
        "c1", "light", ["a", "d"],
        hotbar={"1": "fireball"}, skills=["fireball"],
    )
    result = guard.apply("c1", "light")
    assert result["applied"] is True
    state = guard.state("c1")
    assert state["hotbar"] == {"1": "fireball"}
    assert state["skills"] == ["fireball"]


def test_revert_restores_equipment_hotbar_and_stats():
    guard = lsg.LoadoutSwapGuard()
    guard.register_character(
        "c3",
        items=[
            item("o1", attack=10, set="t1", owner="c3"),
            item("o2", attack=10, set="t1", owner="c3"),
            item("n1", attack=5, set="t2", owner="c3"),
            item("n2", attack=5, set="t2", owner="c3"),
        ],
        equipped=["o1", "o2"],
        bag=["n1", "n2"],
        bag_capacity=2,
        hotbar={"1": "old"},
    )
    guard.save("c3", "new", ["n1", "n2"], hotbar={"1": "new"})
    guard.apply("c3", "new")
    reverted = guard.revert("c3")
    assert reverted["reverted"] is True
    assert set(reverted["equipped"]) == {"o1", "o2"}
    assert reverted["stats"]["total"] == 30
    assert guard.state("c3")["hotbar"] == {"1": "old"}


def test_loadout_can_be_applied_again_after_revert():
    guard = base_character(lsg.LoadoutSwapGuard())
    guard.save("c1", "light", ["a", "d"])
    guard.apply("c1", "light")
    guard.revert("c1")
    again = guard.apply("c1", "light")
    assert again["applied"] is True
    assert again["changed"] is True
    assert set(guard.state("c1")["equipped"]) == {"a", "d"}


def test_preview_does_not_change_equipment_or_reservations():
    guard = base_character(lsg.LoadoutSwapGuard())
    guard.save("c1", "light", ["a", "d"])
    before = guard.state("c1")
    preview = guard.preview("c1", "light")
    assert preview["valid"] is True
    assert guard.state("c1") == before
    assert guard.state("c1")["locked"] is False
