"""Item binding ledger with propagation, unbinding and trade checks."""

import copy


DEFAULT_BIND_PRIORITY = {
    "none": 0,
    "trade": 1,
    "equip": 2,
    "account": 3,
}


class ItemBindingLedger:
    def __init__(self, bind_priority=None):
        self.bind_priority = dict(DEFAULT_BIND_PRIORITY)
        if bind_priority:
            self.bind_priority.update(bind_priority)
        self.items = {}
        self.characters = {}
        self.events = []
        self.seen_events = set()
        self.archive = {}

    def _rank(self, binding):
        return self.bind_priority.get(binding, -1)

    def _claim_event(self, event_id):
        if event_id is None:
            return True
        if event_id in self.seen_events:
            return False
        self.seen_events.add(event_id)
        return True

    def _record_event(self, action, item_id, event_id=None, **details):
        event = {
            "action": action,
            "item_id": item_id,
            "event_id": event_id,
        }
        event.update(details)
        self.events.append(event)
        return event

    def add_character(self, character_id, account_id=None, region="r1"):
        self.characters[character_id] = {
            "character_id": character_id,
            "account_id": account_id,
            "region": region,
            "equipped": set(),
        }
        return self.character_state(character_id)

    def add_item(self, item_id, owner=None, account_id=None, binding="none",
                 sockets=None):
        if binding not in self.bind_priority:
            raise ValueError("unknown binding type: %s" % binding)

        if account_id is None and owner in self.characters:
            account_id = self.characters[owner]["account_id"]

        socket_ids = list(dict.fromkeys(sockets or []))
        bound_to = None
        bound_account = None
        if binding == "account":
            bound_account = account_id
            bound_to = owner
        elif binding != "none":
            bound_to = owner

        self.items[item_id] = {
            "item_id": item_id,
            "owner": owner,
            "account_id": account_id,
            "binding": binding,
            "bound_to": bound_to,
            "bound_account": bound_account,
            "equipped": False,
            "equipped_by": None,
            "sockets": socket_ids,
            "socketed_in": None,
            "enchant_id": None,
            "enchant_source": None,
        }

        for gem_id in socket_ids:
            gem = self.items.get(gem_id)
            if gem is not None:
                gem["socketed_in"] = item_id

        for other in self.items.values():
            if other["item_id"] != item_id and item_id in other["sockets"]:
                self.items[item_id]["socketed_in"] = other["item_id"]
                break

        if binding == "equip":
            self._propagate_equip(
                item_id, source=binding, operator=owner, event_id=None
            )

        return self.item_state(item_id)

    def binding_of(self, item_id):
        item = self.items.get(item_id)
        return None if item is None else item["binding"]

    def item_state(self, item_id):
        item = self.items.get(item_id)
        if item is None:
            return None
        state = dict(item)
        state["sockets"] = list(item["sockets"])
        return state

    def character_state(self, character_id):
        character = self.characters.get(character_id)
        if character is None:
            return None
        state = dict(character)
        state["equipped"] = sorted(character["equipped"])
        return state

    def archived_events(self, character_id):
        return copy.deepcopy(self.archive.get(character_id, []))

    def _apply_binding(self, item_id, binding, source, operator, event_id=None,
                       bound_to=None):
        item = self.items.get(item_id)
        if item is None:
            return {
                "applied": False,
                "binding": None,
                "reason": "item_not_found",
            }

        current = item["binding"]
        if self._rank(binding) <= self._rank(current):
            return {
                "applied": False,
                "binding": current,
                "reason": "already_bound" if binding == current else "not_stricter",
            }

        previous = current
        item["binding"] = binding
        if binding == "none":
            item["bound_to"] = None
            item["bound_account"] = None
        elif binding == "account":
            account_id = item.get("account_id")
            if account_id is None and item.get("owner") in self.characters:
                account_id = self.characters[item["owner"]]["account_id"]
                item["account_id"] = account_id
            if account_id is None and operator in self.characters:
                account_id = self.characters[operator]["account_id"]
                item["account_id"] = account_id
            item["bound_account"] = account_id
            item["bound_to"] = (
                bound_to if bound_to is not None else item.get("owner")
            )
        else:
            item["bound_to"] = (
                bound_to if bound_to is not None else item.get("owner")
            )
            item["bound_account"] = None

        event = self._record_event(
            "bind",
            item_id,
            event_id=event_id,
            source=source,
            operator=operator,
            binding=binding,
            previous_binding=previous,
        )
        return {
            "applied": True,
            "binding": binding,
            "reason": "ok",
            "event": event,
        }

    def bind(self, item_id, binding, source="manual", operator=None,
             event_id=None):
        item = self.items.get(item_id)
        if item is None:
            return {
                "applied": False,
                "binding": None,
                "reason": "item_not_found",
            }
        if binding not in self.bind_priority:
            return {
                "applied": False,
                "binding": item["binding"],
                "reason": "unknown_binding",
            }
        if not self._claim_event(event_id):
            return {
                "applied": False,
                "binding": item["binding"],
                "duplicate": True,
                "reason": "duplicate_event",
            }

        result = self._apply_binding(
            item_id, binding, source=source, operator=operator,
            event_id=event_id
        )
        if result["applied"] and binding == "equip":
            self._propagate_equip(
                item_id, source=source, operator=operator, event_id=event_id
            )
        return result

    def bind_batch(self, item_ids, binding, source="manual", operator=None,
                   event_id_prefix=None):
        results = []
        for index, item_id in enumerate(item_ids):
            event_id = None
            if event_id_prefix is not None:
                event_id = "%s:%s:%s" % (event_id_prefix, index, item_id)
            results.append(
                self.bind(
                    item_id,
                    binding,
                    source=source,
                    operator=operator,
                    event_id=event_id,
                )
            )
        return results

    def _propagate_equip(self, item_id, source="manual", operator=None,
                         event_id=None, visited=None):
        if visited is None:
            visited = set()
        if item_id in visited:
            return []
        visited.add(item_id)

        parent = self.items.get(item_id)
        if parent is None or parent["binding"] != "equip":
            return []

        results = []
        for gem_id in list(parent["sockets"]):
            gem = self.items.get(gem_id)
            if gem is None:
                continue
            if self._rank(gem["binding"]) < self._rank("equip"):
                child_event_id = None
                if event_id is not None:
                    child_event_id = "%s:prop:%s" % (event_id, gem_id)
                if self._claim_event(child_event_id):
                    bound_to = parent.get("bound_to") or gem.get("owner")
                    result = self._apply_binding(
                        gem_id,
                        "equip",
                        source="propagation",
                        operator=operator,
                        event_id=child_event_id,
                        bound_to=bound_to,
                    )
                    results.append(result)
            results.extend(
                self._propagate_equip(
                    gem_id,
                    source=source,
                    operator=operator,
                    event_id=event_id,
                    visited=visited,
                )
            )
        return results

    def _is_equipped(self, item):
        if item.get("equipped"):
            return True
        parent = self.items.get(item.get("socketed_in"))
        return bool(parent and parent.get("equipped"))

    def unbind(self, item_id, operator=None, event_id=None):
        item = self.items.get(item_id)
        if item is None:
            return {
                "unbound": False,
                "reason": "item_not_found",
                "scroll_consumed": False,
            }
        if event_id is not None and event_id in self.seen_events:
            return {
                "unbound": False,
                "duplicate": True,
                "reason": "duplicate_event",
                "scroll_consumed": False,
            }

        if self._is_equipped(item):
            return {
                "unbound": False,
                "reason": "equipped",
                "scroll_consumed": False,
            }
        if item["binding"] == "account":
            return {
                "unbound": False,
                "reason": "account_bound",
                "scroll_consumed": False,
            }
        if item["binding"] == "none":
            return {
                "unbound": False,
                "reason": "not_bound",
                "scroll_consumed": False,
            }

        self._claim_event(event_id)
        previous = item["binding"]
        item["binding"] = "none"
        item["bound_to"] = None
        item["bound_account"] = None
        self._record_event(
            "unbind",
            item_id,
            event_id=event_id,
            source="scroll",
            operator=operator,
            previous_binding=previous,
            binding="none",
        )
        return {
            "unbound": True,
            "reason": "ok",
            "scroll_consumed": True,
            "previous_binding": previous,
        }

    def equip(self, character_id, item_id, event_id=None):
        character = self.characters.get(character_id)
        item = self.items.get(item_id)
        if character is None:
            return {"equipped": False, "reason": "character_not_found"}
        if item is None:
            return {"equipped": False, "reason": "item_not_found"}
        if item["equipped_by"] not in (None, character_id):
            return {"equipped": False, "reason": "equipped_by_another"}

        result = self.bind(
            item_id,
            "equip",
            source="equip",
            operator=character_id,
            event_id=event_id,
        )
        if result.get("duplicate"):
            return {
                "equipped": False,
                "duplicate": True,
                "reason": "duplicate_event",
                "binding": item["binding"],
            }

        if item["owner"] is None:
            item["owner"] = character_id
            if item["account_id"] is None:
                item["account_id"] = character["account_id"]
        item["equipped"] = True
        item["equipped_by"] = character_id
        character["equipped"].add(item_id)

        if item["binding"] == "equip":
            self._propagate_equip(
                item_id, source="equip", operator=character_id, event_id=event_id
            )
        return {
            "equipped": True,
            "binding": item["binding"],
            "reason": "ok",
        }

    def unequip(self, character_id, item_id, event_id=None):
        character = self.characters.get(character_id)
        item = self.items.get(item_id)
        if character is None:
            return {"unequipped": False, "reason": "character_not_found"}
        if item is None:
            return {"unequipped": False, "reason": "item_not_found"}
        if event_id is not None and not self._claim_event(event_id):
            return {
                "unequipped": False,
                "duplicate": True,
                "reason": "duplicate_event",
            }
        if item["equipped_by"] != character_id:
            return {"unequipped": False, "reason": "not_equipped"}

        item["equipped"] = False
        item["equipped_by"] = None
        character["equipped"].discard(item_id)
        if event_id is not None:
            self._record_event(
                "unequip",
                item_id,
                event_id=event_id,
                source="equip",
                operator=character_id,
            )
        return {"unequipped": True, "reason": "ok"}

    def socket(self, item_id, gem_id, event_id=None):
        item = self.items.get(item_id)
        gem = self.items.get(gem_id)
        if item is None or gem is None:
            return {"socketed": False, "reason": "item_not_found"}
        if item_id == gem_id:
            return {"socketed": False, "reason": "same_item"}
        if event_id is not None and not self._claim_event(event_id):
            return {
                "socketed": False,
                "duplicate": True,
                "reason": "duplicate_event",
            }

        old_parent = self.items.get(gem["socketed_in"])
        if old_parent is not None and gem_id in old_parent["sockets"]:
            old_parent["sockets"].remove(gem_id)

        if gem_id not in item["sockets"]:
            item["sockets"].append(gem_id)
        gem["socketed_in"] = item_id

        if item["binding"] == "equip":
            self._propagate_equip(
                item_id,
                source="socket",
                operator=item.get("bound_to"),
                event_id=event_id,
            )

        if event_id is not None:
            self._record_event(
                "socket",
                item_id,
                event_id=event_id,
                source="socket",
                operator=item.get("bound_to"),
                gem_id=gem_id,
            )
        return {"socketed": True, "reason": "ok"}

    def unsocket(self, item_id, gem_id, event_id=None):
        item = self.items.get(item_id)
        gem = self.items.get(gem_id)
        if item is None or gem is None:
            return {"unsocketed": False, "reason": "item_not_found"}
        if event_id is not None and not self._claim_event(event_id):
            return {
                "unsocketed": False,
                "duplicate": True,
                "reason": "duplicate_event",
            }
        if gem_id not in item["sockets"]:
            return {"unsocketed": False, "reason": "not_socketed"}

        item["sockets"].remove(gem_id)
        if gem["socketed_in"] == item_id:
            gem["socketed_in"] = None
        if event_id is not None:
            self._record_event(
                "unsocket",
                item_id,
                event_id=event_id,
                source="socket",
                operator=item.get("bound_to"),
                gem_id=gem_id,
            )
        return {"unsocketed": True, "reason": "ok"}

    def enchant(self, item_id, enchant_id, source_character, event_id=None):
        item = self.items.get(item_id)
        if item is None:
            return {
                "applied": False,
                "binding": None,
                "reason": "item_not_found",
            }
        if event_id is not None and event_id in self.seen_events:
            return {
                "applied": False,
                "binding": item["binding"],
                "duplicate": True,
                "reason": "duplicate_event",
            }

        result = self.bind(
            item_id,
            "equip",
            source="enchant",
            operator=source_character,
            event_id=event_id,
        )
        item["enchant_id"] = enchant_id
        item["enchant_source"] = source_character

        if item["binding"] == "equip":
            self._propagate_equip(
                item_id,
                source="enchant",
                operator=source_character,
                event_id=event_id,
            )
        result["binding"] = item["binding"]
        result["bound_to"] = item["bound_to"]
        result["enchant_id"] = enchant_id
        return result

    def trade_check(self, item_id, buyer_account=None, stage="list",
                    event_id=None):
        item = self.items.get(item_id)
        if item is None:
            return {"allowed": False, "reason": "item_not_found"}

        binding = item["binding"]
        if binding != "none":
            return {"allowed": False, "reason": "bound_%s" % binding}
        if item.get("equipped"):
            return {"allowed": False, "reason": "equipped"}
        return {"allowed": True, "reason": "ok"}

    def mail(self, item_id, from_account, to_character, event_id=None):
        item = self.items.get(item_id)
        if item is None:
            return {"sent": False, "reason": "item_not_found"}
        recipient = self.characters.get(to_character)
        if recipient is None:
            return {"sent": False, "reason": "recipient_not_found"}
        if event_id is not None and event_id in self.seen_events:
            return {
                "sent": False,
                "duplicate": True,
                "reason": "duplicate_event",
            }

        if item["binding"] == "account":
            account_id = item.get("bound_account") or item.get("account_id")
            if (
                from_account != account_id
                or recipient.get("account_id") != account_id
            ):
                return {"sent": False, "reason": "account_mismatch"}

        self._claim_event(event_id)
        item["owner"] = to_character
        if item["binding"] == "account":
            item["account_id"] = recipient.get("account_id")
            item["bound_account"] = recipient.get("account_id")
            item["bound_to"] = to_character
        elif item["binding"] == "none":
            item["account_id"] = recipient.get("account_id")

        if event_id is not None:
            self._record_event(
                "mail",
                item_id,
                event_id=event_id,
                source="mail",
                operator=from_account,
                to_character=to_character,
                binding=item["binding"],
            )
        return {
            "sent": True,
            "reason": "ok",
            "binding": item["binding"],
        }

    def transfer_region(self, character_id, region, event_id=None):
        character = self.characters.get(character_id)
        if character is None:
            return {"transferred": False, "reason": "character_not_found"}
        if event_id is not None and not self._claim_event(event_id):
            return {
                "transferred": False,
                "duplicate": True,
                "reason": "duplicate_event",
            }

        character["region"] = region
        for item in self.items.values():
            if item["owner"] == character_id:
                item["region"] = region

        if event_id is not None:
            self._record_event(
                "transfer_region",
                None,
                event_id=event_id,
                source="region_transfer",
                operator=character_id,
                region=region,
            )
        return {
            "transferred": True,
            "region": region,
            "reason": "ok",
        }

    def delete_character(self, character_id, archive=True):
        character = self.characters.get(character_id)
        if character is None:
            return {"deleted": False, "reason": "character_not_found"}

        affected = set()
        for item_id, item in self.items.items():
            if (
                item.get("owner") == character_id
                or item.get("equipped_by") == character_id
                or item.get("bound_to") == character_id
            ):
                affected.add(item_id)

        archived = 0
        if archive:
            archived_events = []
            for event in self.events:
                if (
                    event.get("operator") == character_id
                    or event.get("character_id") == character_id
                    or event.get("item_id") in affected
                ):
                    archived_events.append(copy.deepcopy(event))
            self.archive[character_id] = archived_events
            archived = len(archived_events)

        for item in self.items.values():
            if item.get("equipped_by") == character_id:
                item["equipped"] = False
                item["equipped_by"] = None
            if (
                item["binding"] in ("equip", "trade")
                and item.get("bound_to") == character_id
            ):
                item["binding"] = "none"
                item["bound_to"] = None
                item["bound_account"] = None
            elif (
                item["binding"] == "account"
                and item.get("bound_to") == character_id
            ):
                item["bound_to"] = None
            if item.get("owner") == character_id:
                item["owner"] = None

        del self.characters[character_id]
        return {
            "deleted": True,
            "archived_events": archived,
            "reason": "ok",
        }

    def history(self, item_id=None):
        if item_id is None:
            return copy.deepcopy(self.events)
        return copy.deepcopy(
            [event for event in self.events if event.get("item_id") == item_id]
        )
