"""Guild recruitment applications, approvals and capacity."""


class GuildApplicationRoster:
    def __init__(self, application_ttl=7 * 86400.0, timezone="+08:00"):
        self.application_ttl = application_ttl
        self.timezone = timezone
        self.guilds = {}
        self.accounts = {}
        self.character_account = {}
        self.application_records = {}
        self.event_log = {}
        self.next_application = 1

    def add_guild(self, guild_id, leader, capacity=10, officers=None, members=None):
        self.guilds[guild_id] = {
            "leader": leader,
            "officers": list(officers or []),
            "capacity": capacity,
            "members": list(members or []),
        }
        self.event_log.setdefault(guild_id, [])

    def add_account(self, account_id, characters, guild=None):
        existing = self.accounts.get(account_id, {})
        self.accounts[account_id] = {
            "characters": list(characters),
            "guild": guild,
            "blacklisted": existing.get("blacklisted", False),
        }
        for character in characters:
            self.character_account[character] = account_id

    def set_blacklist(self, account_id, blacklisted=True):
        account = self.accounts.setdefault(account_id, {
            "characters": [],
            "guild": None,
            "blacklisted": False,
        })
        account["blacklisted"] = blacklisted

    def apply(self, guild_id, player_id, account_id=None, note="", now=0.0):
        if guild_id not in self.guilds:
            return {"ok": False, "reason": "no_guild"}
        if account_id is None:
            account_id = self.character_account.get(player_id)
        account = self.accounts.get(account_id) if account_id else None
        if account is not None and account.get("blacklisted"):
            return {"ok": False, "reason": "blacklisted"}
        existing = self._find_pending(guild_id, player_id)
        if existing is not None:
            existing["note"] = note
            existing["applied_at"] = now
            existing["account_id"] = account_id
            return {"ok": True, "application_id": existing["application_id"],
                    "duplicate": True}
        application_id = "app%d" % self.next_application
        self.next_application += 1
        record = {
            "application_id": application_id,
            "guild_id": guild_id,
            "player_id": player_id,
            "account_id": account_id,
            "note": note,
            "status": "pending",
            "applied_at": now,
        }
        self.application_records[application_id] = record
        self._log_event(guild_id, record, None, "pending", player_id, now)
        return {"ok": True, "application_id": application_id, "duplicate": False}

    def approve(self, guild_id, application_id, actor, now=0.0):
        guild = self.guilds.get(guild_id)
        if guild is None:
            return {"approved": False, "reason": "no_guild",
                    "application_id": application_id}
        if not self._is_manager(guild, actor):
            return {"approved": False, "reason": "not_authorized",
                    "application_id": application_id}
        record = self.application_records.get(application_id)
        if record is None or record["guild_id"] != guild_id:
            return {"approved": False, "reason": "not_found",
                    "application_id": application_id}
        if record["status"] == "pending" and self._is_expired(record, now):
            self._transition(guild_id, record, "expired", actor, now)
            return {"approved": False, "reason": "expired",
                    "application_id": application_id}
        if record["status"] != "pending":
            return {"approved": False, "reason": "not_pending",
                    "application_id": application_id}
        if self._in_any_guild(record):
            return {"approved": False, "reason": "already_in_guild",
                    "application_id": application_id}
        if len(guild["members"]) >= guild["capacity"]:
            return {"approved": False, "reason": "full",
                    "application_id": application_id}
        guild["members"].append(record["player_id"])
        account = self.accounts.get(record["account_id"])
        if account is not None:
            account["guild"] = guild_id
        self._transition(guild_id, record, "approved", actor, now)
        return {"approved": True, "reason": None,
                "application_id": application_id}

    def reject(self, guild_id, application_id, actor, reason="", now=0.0):
        guild = self.guilds.get(guild_id)
        if guild is None:
            return {"rejected": False, "reason": "no_guild",
                    "application_id": application_id}
        if not self._is_manager(guild, actor):
            return {"rejected": False, "reason": "not_authorized",
                    "application_id": application_id}
        record = self.application_records.get(application_id)
        if record is None or record["guild_id"] != guild_id:
            return {"rejected": False, "reason": "not_found",
                    "application_id": application_id}
        if record["status"] == "pending" and self._is_expired(record, now):
            self._transition(guild_id, record, "expired", actor, now)
            return {"rejected": False, "reason": "expired",
                    "application_id": application_id}
        if record["status"] != "pending":
            return {"rejected": False, "reason": "not_pending",
                    "application_id": application_id}
        record["reject_reason"] = reason
        self._transition(guild_id, record, "rejected", actor, now)
        return {"rejected": True, "reason": None,
                "application_id": application_id}

    def approve_batch(self, guild_id, application_ids, actor, now=0.0):
        def sort_key(application_id):
            record = self.application_records.get(application_id)
            if record is None:
                return (1, 0.0)
            return (0, record["applied_at"])

        results = []
        for application_id in sorted(application_ids, key=sort_key):
            results.append(self.approve(guild_id, application_id, actor, now=now))
        return results

    def capacity(self, guild_id):
        guild = self.guilds[guild_id]
        pending = sum(1 for record in self.application_records.values()
                      if record["guild_id"] == guild_id
                      and record["status"] == "pending")
        members = len(guild["members"])
        return {
            "limit": guild["capacity"],
            "members": members,
            "pending": pending,
            "available": guild["capacity"] - members,
        }

    def applications(self, guild_id):
        records = [record for record in self.application_records.values()
                   if record["guild_id"] == guild_id]
        records.sort(key=lambda record: record["applied_at"], reverse=True)
        return [dict(record) for record in records]

    def members(self, guild_id):
        return list(self.guilds[guild_id]["members"])

    def transfer_leadership(self, guild_id, new_leader):
        guild = self.guilds[guild_id]
        guild["leader"] = new_leader
        if new_leader in guild["officers"]:
            guild["officers"].remove(new_leader)

    def expand(self, guild_id, new_capacity):
        self.guilds[guild_id]["capacity"] = new_capacity

    def events_for(self, guild_id):
        return list(self.event_log.get(guild_id, []))

    def _find_pending(self, guild_id, player_id):
        for record in self.application_records.values():
            if (record["guild_id"] == guild_id
                    and record["player_id"] == player_id
                    and record["status"] == "pending"):
                return record
        return None

    def _is_manager(self, guild, actor):
        return actor == guild["leader"] or actor in guild["officers"]

    def _is_expired(self, record, now):
        return now - record["applied_at"] > self.application_ttl

    def _in_any_guild(self, record):
        account = self.accounts.get(record["account_id"])
        if account is not None and account.get("guild"):
            return True
        player_id = record["player_id"]
        return any(player_id in guild["members"]
                   for guild in self.guilds.values())

    def _transition(self, guild_id, record, new_state, actor, now):
        old_state = record["status"]
        record["status"] = new_state
        self._log_event(guild_id, record, old_state, new_state, actor, now)

    def _log_event(self, guild_id, record, old_state, new_state, actor, now):
        guild = self.guilds[guild_id]
        audience = [guild["leader"]]
        audience.extend(officer for officer in guild["officers"]
                        if officer != guild["leader"])
        self.event_log.setdefault(guild_id, []).append({
            "application_id": record["application_id"],
            "player_id": record["player_id"],
            "old_state": old_state,
            "new_state": new_state,
            "actor": actor,
            "audience": audience,
            "at": now,
        })
