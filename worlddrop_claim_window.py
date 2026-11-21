"""World drop ownership, protection window and claim locking."""


class WorlddropClaimWindow:
    def __init__(self, protection=60.0, damage_share=0.6):
        self.protection = protection
        self.damage_share = damage_share
        self.kills = {}
        self.drops = {}
        self.corpses = {}
        self.claims = {}
        self.quest_counts = {}
        self.request_results = {}
        self.player_factions = {}
        self.player_maps = {}
        self.mail = []

    def add_player(self, player, faction, map_id=None):
        self.player_factions[player] = faction
        if map_id is not None:
            self.player_maps[player] = map_id

    def register_kill(self, kill_id, mob_id, damage, last_hit_party,
                      party_members, factions, map_id, drops, now=0.0):
        total_damage = sum(damage.values()) or 0

        def effective_share(party_id):
            if total_damage <= 0:
                return 0.0
            return damage.get(party_id, 0) / total_damage

        ranked = sorted(
            damage,
            key=lambda party_id: (effective_share(party_id),
                                  party_id == last_hit_party),
            reverse=True,
        )
        owner_party = None
        for party_id in ranked:
            if effective_share(party_id) >= self.damage_share:
                owner_party = party_id
                break
        if owner_party is None:
            owner_party = last_hit_party

        expires_at = now + self.protection
        corpse_id = "corpse-" + kill_id

        kill = {
            "kill_id": kill_id,
            "mob_id": mob_id,
            "map_id": map_id,
            "owner_party": owner_party,
            "last_hit_party": last_hit_party,
            "party_members": {party: list(members)
                              for party, members in party_members.items()},
            "factions": dict(factions),
            "damage": dict(damage),
            "drops": [drop["drop_id"] for drop in drops],
            "corpse_id": corpse_id,
            "spawned_at": now,
            "expires_at": expires_at,
            "spawn_frame": now,
            "expiry_frame": expires_at,
        }
        self.kills[kill_id] = kill

        self.corpses[corpse_id] = {
            "corpse_id": corpse_id,
            "kill_id": kill_id,
            "map_id": map_id,
            "spawned_at": now,
            "expires_at": expires_at,
            "exists": True,
            "drops": [drop["drop_id"] for drop in drops],
        }

        owner_members = party_members.get(owner_party, [])
        mail_to = owner_members[0] if owner_members else None
        for member in owner_members:
            self.player_maps[member] = map_id

        for drop in drops:
            drop_id = drop["drop_id"]
            self.drops[drop_id] = {
                "drop_id": drop_id,
                "item_id": drop["item_id"],
                "quantity": drop.get("quantity", 1),
                "reachable": drop.get("reachable", True),
                "kill_id": kill_id,
                "corpse_id": corpse_id,
                "map_id": map_id,
                "owner_party": owner_party,
                "mail_to": mail_to,
                "spawned_at": now,
                "expires_at": expires_at,
                "spawn_frame": now,
                "expiry_frame": expires_at,
                "claimed": False,
                "claimed_by": None,
                "claimed_at": None,
            }

        return {
            "kill_id": kill_id,
            "corpse_id": corpse_id,
            "owner_party": owner_party,
            "expires_at": expires_at,
        }

    def _faction_of(self, player, kill):
        if player in self.player_factions:
            return self.player_factions[player]
        for party_id, members in kill["party_members"].items():
            if player in members:
                return kill["factions"].get(party_id)
        return None

    def _effective_owner_party(self, kill):
        owner_party = kill["owner_party"]
        if owner_party is None:
            return None
        members = kill["party_members"].get(owner_party, [])
        if not members:
            return None
        for member in members:
            if self.player_maps.get(member) == kill["map_id"]:
                return owner_party
        return None

    def rights(self, drop_id, player, now=0.0):
        drop = self.drops[drop_id]
        kill = self.kills[drop["kill_id"]]

        result = {
            "drop_id": drop_id,
            "allowed": False,
            "owner_party": kill["owner_party"],
            "expires_at": drop["expires_at"],
        }

        if now >= drop["expires_at"]:
            result["reason"] = "expired"
            return result

        if drop["claimed"]:
            result["reason"] = "already_claimed"
            return result

        player_faction = self._faction_of(player, kill)
        owner_party = self._effective_owner_party(kill)

        if owner_party is not None:
            owner_faction = kill["factions"].get(owner_party)
            if player_faction != owner_faction:
                result["reason"] = "faction"
                return result

            members = kill["party_members"].get(owner_party, [])
            if player in members:
                if self.player_maps.get(player) != kill["map_id"]:
                    result["reason"] = "off_map"
                    return result
                result["allowed"] = True
                result["reason"] = "ok"
                return result

            result["reason"] = "protected"
            return result

        result["owner_party"] = None
        if self.player_maps.get(player) is not None and \
                self.player_maps.get(player) != kill["map_id"]:
            result["reason"] = "off_map"
            return result
        result["allowed"] = True
        result["reason"] = "ok"
        return result

    def claim(self, drop_id, player, now=0.0, request_id=None,
              client_now=None, reachable=True):
        drop = self.drops[drop_id]

        if request_id is not None and request_id in self.request_results:
            cached = dict(self.request_results[request_id])
            cached["duplicate"] = True
            return cached

        decision = self.rights(drop_id, player, now)
        if not decision["allowed"]:
            return {
                "drop_id": drop_id,
                "claimed": False,
                "reason": decision["reason"],
                "quest_count": len(self.claims),
            }

        if not reachable or not drop["reachable"]:
            return {
                "drop_id": drop_id,
                "claimed": False,
                "reason": "unreachable",
                "quest_count": len(self.claims),
            }

        drop["claimed"] = True
        drop["claimed_by"] = player
        drop["claimed_at"] = now
        self.claims[drop_id] = {
            "drop_id": drop_id,
            "player": player,
            "at": now,
            "request_id": request_id,
        }

        result = {
            "drop_id": drop_id,
            "claimed": True,
            "reason": "ok",
            "quest_count": len(self.claims),
        }
        if request_id is not None:
            self.request_results[request_id] = dict(result)
        return result

    def expire(self, now=0.0):
        expired = []
        mailed = []

        for drop_id in sorted(self.drops):
            drop = self.drops[drop_id]
            if drop["claimed"] or now < drop["expires_at"]:
                continue

            expired.append(drop_id)
            drop["claimed"] = True
            drop["claimed_by"] = None
            drop["claimed_at"] = now

            entry = {
                "drop_id": drop_id,
                "item_id": drop["item_id"],
                "quantity": drop["quantity"],
                "to": drop["mail_to"],
                "kill_id": drop["kill_id"],
            }
            self.mail.append(entry)
            mailed.append(entry)

        for corpse in self.corpses.values():
            if not corpse["exists"]:
                continue
            corpse_drops = self.kills[corpse["kill_id"]]["drops"]
            if now >= corpse["expires_at"] or \
                    all(self.drops[drop_id]["claimed"]
                        for drop_id in corpse_drops):
                corpse["exists"] = False

        return {"expired": expired, "mailed": mailed}

    def party_leave(self, party_id, member, now=0.0):
        for kill in self.kills.values():
            members = kill["party_members"].get(party_id)
            if members and member in members:
                members.remove(member)

    def teleport(self, player, map_id, now=0.0):
        self.player_maps[player] = map_id

    def corpse_state(self, corpse_id, now=0.0):
        corpse = self.corpses[corpse_id]
        return {
            "corpse_id": corpse_id,
            "exists": corpse["exists"],
            "expires_at": corpse["expires_at"],
        }

    def mail_queue(self):
        return list(self.mail)

    def snapshot(self, now=0.0):
        return {
            "protection": self.protection,
            "damage_share": self.damage_share,
            "player_factions": dict(self.player_factions),
            "player_maps": dict(self.player_maps),
            "kills": {key: dict(value) for key, value in self.kills.items()},
            "drops": {key: dict(value) for key, value in self.drops.items()},
            "corpses": {key: dict(value)
                        for key, value in self.corpses.items()},
            "claims": dict(self.claims),
            "quest_counts": dict(self.quest_counts),
            "request_results": {key: dict(value)
                                for key, value in
                                self.request_results.items()},
            "mail": list(self.mail),
            "snapshot_at": now,
        }

    def restore(self, snapshot, now=0.0):
        self.protection = snapshot["protection"]
        self.damage_share = snapshot["damage_share"]
        self.player_factions = dict(snapshot["player_factions"])
        self.player_maps = dict(snapshot["player_maps"])
        self.kills = {key: dict(value)
                     for key, value in snapshot["kills"].items()}
        self.drops = {key: dict(value)
                     for key, value in snapshot["drops"].items()}
        self.corpses = {key: dict(value)
                      for key, value in snapshot["corpses"].items()}
        self.claims = dict(snapshot["claims"])
        self.quest_counts = dict(snapshot.get("quest_counts", {}))
        self.request_results = {
            key: dict(value)
            for key, value in snapshot["request_results"].items()
        }
        self.mail = list(snapshot["mail"])

        elapsed = now - snapshot.get("snapshot_at", now)
        if elapsed > 0:
            for kill in self.kills.values():
                kill["expires_at"] += elapsed
                kill["expiry_frame"] = kill["expires_at"]
            for drop in self.drops.values():
                drop["expires_at"] += elapsed
                drop["expiry_frame"] = drop["expires_at"]
            for corpse in self.corpses.values():
                corpse["expires_at"] += elapsed
