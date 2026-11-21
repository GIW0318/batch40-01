"""Transactional equipment loadout swapping."""

from __future__ import annotations

import copy
import threading
from typing import Any, Iterable, Mapping


class LoadoutSwapGuard:
    """Guard equipment loadouts with reservations and transactional swaps."""

    def __init__(self):
        self._lock = threading.RLock()
        self._characters: dict[Any, dict[str, Any]] = {}

    def register_character(
        self,
        character_id,
        items=None,
        equipped=None,
        bag=None,
        bag_capacity=6,
        hotbar=None,
        skills=None,
    ):
        with self._lock:
            item_map: dict[Any, dict[str, Any]] = {}
            if isinstance(items, Mapping):
                source_items = items.items()
            else:
                source_items = (
                    (entry.get("item_id"), entry) for entry in (items or [])
                )

            for item_id, entry in source_items:
                if item_id is None or not isinstance(entry, Mapping):
                    continue
                stored = copy.deepcopy(dict(entry))
                stored["item_id"] = item_id
                stored.setdefault("slot", "armor")
                stored.setdefault("attack", 0)
                stored.setdefault("owner", character_id)
                stored.setdefault("deleted", False)
                item_map[item_id] = stored

            character = {
                "items": item_map,
                "equipped": list(equipped or []),
                "bag": list(bag or []),
                "bag_capacity": int(bag_capacity),
                "hotbar": copy.deepcopy(dict(hotbar or {})),
                "skills": list(skills or []),
                "loadouts": {},
                "last_snapshot": None,
                "current_loadout": None,
                "locked": False,
            }
            self._characters[character_id] = character
            return {"registered": True, "character_id": character_id}

    def add_item(self, character_id, item):
        with self._lock:
            character = self._characters.get(character_id)
            if character is None:
                return {"added": False, "reason": "unknown character"}
            if not isinstance(item, Mapping) or item.get("item_id") is None:
                return {"added": False, "reason": "invalid item"}

            item_id = item["item_id"]
            existing = character["items"].get(item_id)
            if existing is not None and not existing.get("deleted"):
                return {"added": False, "reason": f"duplicate:{item_id}"}

            if (
                item_id not in character["bag"]
                and item_id not in character["equipped"]
                and len(character["bag"]) >= character["bag_capacity"]
            ):
                return {"added": False, "reason": "not enough bag slots"}

            stored = copy.deepcopy(dict(item))
            stored["item_id"] = item_id
            stored.setdefault("slot", "armor")
            stored.setdefault("attack", 0)
            stored.setdefault("owner", character_id)
            stored.setdefault("deleted", False)
            character["items"][item_id] = stored

            if item_id not in character["bag"] and item_id not in character["equipped"]:
                character["bag"].append(item_id)

            return {"added": True, "item_id": item_id}

    def delete_item(self, character_id, item_id):
        with self._lock:
            character = self._characters.get(character_id)
            if character is None or item_id not in character["items"]:
                return {"deleted": False, "reason": "unknown item"}

            character["items"][item_id]["deleted"] = True
            character["equipped"] = [
                equipped_id
                for equipped_id in character["equipped"]
                if equipped_id != item_id
            ]
            character["bag"] = [
                bag_id for bag_id in character["bag"] if bag_id != item_id
            ]
            return {"deleted": True, "item_id": item_id}

    def stats(self, character_id):
        with self._lock:
            character = self._require_character(character_id)
            return self._calculate_stats(character)

    def state(self, character_id):
        with self._lock:
            character = self._require_character(character_id)
            return {
                "equipped": list(character["equipped"]),
                "bag": list(character["bag"]),
                "bag_capacity": character["bag_capacity"],
                "hotbar": copy.deepcopy(character["hotbar"]),
                "skills": list(character["skills"]),
                "stats": self._calculate_stats(character),
                "locked": bool(character["locked"]),
            }

    def save(self, character_id, name, item_ids, hotbar=None, skills=None):
        with self._lock:
            character = self._characters.get(character_id)
            if character is None:
                return {"saved": False, "reason": "unknown character"}

            selected = list(item_ids or [])
            errors = self._validate_loadout_items(
                character_id,
                character,
                selected,
                exclude_loadout=name,
                include_reservations=True,
            )
            if errors:
                return {"saved": False, "reason": errors[0], "errors": errors}

            character["loadouts"][name] = {
                "item_ids": selected,
                "hotbar": copy.deepcopy(dict(hotbar or {})),
                "skills": list(skills or []),
            }
            return {
                "saved": True,
                "name": name,
                "item_ids": list(selected),
            }

    def preview(self, character_id, name):
        with self._lock:
            character = self._characters.get(character_id)
            if character is None:
                return self._invalid_preview("unknown character")
            loadout = character["loadouts"].get(name)
            if loadout is None:
                return self._invalid_preview(f"unknown loadout:{name}")

            added, removed, bag_slots_needed, free_slots = self._swap_diff(
                character, loadout["item_ids"]
            )
            errors = self._validate_loadout_items(
                character_id,
                character,
                loadout["item_ids"],
                exclude_loadout=name,
                include_reservations=True,
            )
            if bag_slots_needed > free_slots:
                errors.append("not enough bag slots")

            return {
                "valid": not errors,
                "name": name,
                "added": added,
                "removed": removed,
                "bag_slots_needed": bag_slots_needed,
                "free_slots": free_slots,
                "errors": errors,
            }

    def apply(self, character_id, name, context=None):
        with self._lock:
            character = self._characters.get(character_id)
            if character is None:
                return {"applied": False, "reason": "unknown character"}

            context = dict(context or {})
            if context.get("in_combat"):
                return {"applied": False, "reason": "in_combat"}

            cooldown_until = context.get("cooldown_until")
            if cooldown_until is not None:
                now = context.get("now", 0)
                try:
                    on_cooldown = now < cooldown_until
                except TypeError:
                    on_cooldown = True
                if on_cooldown:
                    return {"applied": False, "reason": "cooldown"}

            loadout = character["loadouts"].get(name)
            if loadout is None:
                return {
                    "applied": False,
                    "reason": f"unknown loadout:{name}",
                    "errors": [f"unknown loadout:{name}"],
                }

            added, removed, bag_slots_needed, free_slots = self._swap_diff(
                character, loadout["item_ids"]
            )
            errors = self._validate_loadout_items(
                character_id,
                character,
                loadout["item_ids"],
                exclude_loadout=name,
                include_reservations=True,
            )
            if bag_slots_needed > free_slots:
                errors.append("not enough bag slots")
            if errors:
                return {
                    "applied": False,
                    "reason": errors[0],
                    "errors": errors,
                }

            changed = bool(added or removed)
            hotbar_changed = character["hotbar"] != loadout["hotbar"]
            skills_changed = character["skills"] != loadout["skills"]
            snapshot = self._snapshot(character)

            character["locked"] = True
            try:
                added_set = set(added)
                new_bag = [
                    item_id
                    for item_id in character["bag"]
                    if item_id not in added_set
                ]
                for item_id in removed:
                    if item_id not in new_bag:
                        new_bag.append(item_id)

                character["equipped"] = list(loadout["item_ids"])
                character["bag"] = new_bag
                character["hotbar"] = copy.deepcopy(loadout["hotbar"])
                character["skills"] = list(loadout["skills"])

                if context.get("fail_after"):
                    raise RuntimeError("forced transaction failure")
                new_stats = self._calculate_stats(character)
            except Exception:
                self._restore(character, snapshot)
                character["locked"] = False
                return {
                    "applied": False,
                    "reason": "transaction_failed",
                    "errors": ["transaction_failed"],
                }

            if changed or hotbar_changed or skills_changed:
                character["last_snapshot"] = snapshot
            character["current_loadout"] = name
            character["locked"] = False

            return {
                "applied": True,
                "changed": changed,
                "added": added,
                "removed": removed,
                "stats": new_stats,
            }

    def revert(self, character_id):
        with self._lock:
            character = self._characters.get(character_id)
            if character is None:
                return {
                    "reverted": False,
                    "reason": "unknown character",
                    "equipped": [],
                    "stats": {"attack": 0, "set_bonus": 0, "total": 0},
                }

            snapshot = character["last_snapshot"]
            if snapshot is None:
                return {
                    "reverted": False,
                    "reason": "nothing to revert",
                    "equipped": list(character["equipped"]),
                    "stats": self._calculate_stats(character),
                }

            self._restore(character, snapshot)
            character["last_snapshot"] = None
            character["locked"] = False
            return {
                "reverted": True,
                "equipped": list(character["equipped"]),
                "stats": self._calculate_stats(character),
            }

    def _require_character(self, character_id):
        character = self._characters.get(character_id)
        if character is None:
            raise KeyError(f"unknown character:{character_id}")
        return character

    @staticmethod
    def _invalid_preview(reason):
        return {
            "valid": False,
            "added": [],
            "removed": [],
            "bag_slots_needed": 0,
            "free_slots": 0,
            "errors": [reason],
        }

    @staticmethod
    def _validate_loadout_items(
        character_id,
        character,
        item_ids: Iterable[Any],
        exclude_loadout=None,
        include_reservations=False,
    ):
        errors = []
        seen = set()
        has_two_handed = False
        has_offhand = False

        for item_id in item_ids:
            if item_id in seen:
                errors.append(f"duplicate:{item_id}")
                continue
            seen.add(item_id)

            entry = character["items"].get(item_id)
            if entry is None:
                errors.append(f"not owned:{item_id}")
                continue
            if entry.get("deleted"):
                errors.append(f"deleted:{item_id}")
                continue
            if entry.get("owner") not in (None, character_id):
                errors.append(f"not owned:{item_id}")
                continue
            if entry.get("bound_to") not in (None, character_id):
                errors.append(f"not owned:{item_id}")
                continue

            if entry.get("two_handed"):
                has_two_handed = True
            if entry.get("slot") == "offhand":
                has_offhand = True

            if include_reservations:
                other_loadout = LoadoutSwapGuard._loadout_using_item(
                    character, item_id, exclude_loadout
                )
                if other_loadout is not None:
                    errors.append(f"item already in loadout:{item_id}")

        if has_two_handed and has_offhand:
            errors.append("two-handed with offhand")

        return errors

    @staticmethod
    def _loadout_using_item(character, item_id, exclude_loadout=None):
        for loadout_name, loadout in character["loadouts"].items():
            if loadout_name == exclude_loadout:
                continue
            if item_id in loadout["item_ids"]:
                return loadout_name
        return None

    @staticmethod
    def _swap_diff(character, target_item_ids):
        equipped = character["equipped"]
        bag = character["bag"]
        target = list(target_item_ids)

        target_set = set(target)
        equipped_set = set(equipped)
        added = [item_id for item_id in target if item_id not in equipped_set]
        removed = [item_id for item_id in equipped if item_id not in target_set]

        bag_set = set(bag)
        added_from_bag = sum(1 for item_id in added if item_id in bag_set)
        bag_slots_needed = max(0, len(removed) - added_from_bag)
        free_slots = character["bag_capacity"] - len(bag)
        return added, removed, bag_slots_needed, free_slots

    @staticmethod
    def _calculate_stats(character):
        attack = 0
        set_counts: dict[Any, int] = {}
        seen = set()

        for item_id in character["equipped"]:
            if item_id in seen:
                continue
            seen.add(item_id)
            entry = character["items"].get(item_id)
            if entry is None or entry.get("deleted"):
                continue

            attack += entry.get("attack", 0)
            set_name = entry.get("set")
            if set_name is not None:
                set_counts[set_name] = set_counts.get(set_name, 0) + 1

        set_bonus = sum(10 for count in set_counts.values() if count >= 2)
        return {
            "attack": attack,
            "set_bonus": set_bonus,
            "total": attack + set_bonus,
        }

    @staticmethod
    def _snapshot(character):
        return {
            "equipped": list(character["equipped"]),
            "bag": list(character["bag"]),
            "hotbar": copy.deepcopy(character["hotbar"]),
            "skills": list(character["skills"]),
            "current_loadout": character["current_loadout"],
        }

    @staticmethod
    def _restore(character, snapshot):
        character["equipped"] = list(snapshot["equipped"])
        character["bag"] = list(snapshot["bag"])
        character["hotbar"] = copy.deepcopy(snapshot["hotbar"])
        character["skills"] = list(snapshot["skills"])
        character["current_loadout"] = snapshot["current_loadout"]
