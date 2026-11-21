"""Personal quest sharing across party members."""

import math


class QuestShareRelay:
    def __init__(self, quests=None, max_distance=50.0, request_ttl=60.0, period_seconds=86400.0):
        self.quests = dict(quests or {})
        self.max_distance = max_distance
        self.request_ttl = request_ttl
        self.period_seconds = period_seconds
        self.characters = {}
        self.parties = {}
        self.requests = {}
        self.records = {}
        self.daily_counts = {}

    def add_character(self, character_id, faction="neutral", phase=1, map_id="m1",
                      x=0.0, y=0.0, completed=None):
        self.characters[character_id] = {
            "faction": faction,
            "phase": phase,
            "map_id": map_id,
            "x": x,
            "y": y,
            "completed": list(completed or []),
        }

    def add_party(self, party_id, leader, members=None):
        self.parties[party_id] = {
            "leader": leader,
            "members": list(members if members is not None else [leader]),
            "disbanded": False,
        }

    def set_leader(self, party_id, leader):
        self.parties[party_id]["leader"] = leader

    def disband(self, party_id):
        if party_id in self.parties:
            self.parties[party_id]["disbanded"] = True

    def share(self, request_id, party_id, from_character, quest_id, now=0.0):
        if request_id in self.requests:
            return self.requests[request_id]["result"]

        party = self.parties[party_id]
        self._ensure_record(from_character, quest_id, request_id)

        accepted = []
        rejected = {}
        for member_id in party["members"]:
            if member_id == from_character:
                continue
            reason = self._reject_reason(from_character, member_id, quest_id)
            if reason is None:
                accepted.append(member_id)
            else:
                rejected[member_id] = reason

        result = {"accepted": accepted, "rejected": rejected}
        self.requests[request_id] = {
            "party_id": party_id,
            "from_character": from_character,
            "quest_id": quest_id,
            "phase": self.characters[from_character]["phase"],
            "created_at": now,
            "expires_at": now + self.request_ttl,
            "result": result,
            "accepted_by": set(),
        }
        return result

    def accept(self, request_id, character_id, now=0.0):
        request = self.requests.get(request_id)
        if request is None:
            return {"accepted": False, "reason": "unknown_request"}
        if character_id in request["accepted_by"]:
            return {"accepted": False, "reason": "already_accepted"}
        party = self.parties[request["party_id"]]
        if party["disbanded"]:
            return {"accepted": False, "reason": "party_disbanded"}
        if now > request["expires_at"]:
            return {"accepted": False, "reason": "expired"}
        if character_id not in request["result"]["accepted"]:
            return {"accepted": False, "reason": "not_invited"}

        target = self.characters[character_id]
        if target["phase"] != request["phase"]:
            return {"accepted": False, "reason": "phase"}
        quest_id = request["quest_id"]
        for prerequisite in self.quests.get(quest_id, {}).get("requires", []):
            if prerequisite not in target["completed"]:
                return {"accepted": False, "reason": "prerequisite"}

        self._ensure_record(character_id, quest_id, request_id)
        request["accepted_by"].add(character_id)
        return {"accepted": True}

    def add_progress(self, character_id, quest_id, amount, now=0.0):
        record = self.records.get(character_id, {}).get(quest_id)
        if record is not None and record["status"] == "active":
            record["progress"] += amount
            return {"quest_id": quest_id, "progress": record["progress"]}
        return {"quest_id": quest_id, "progress": 0}

    def progress(self, character_id, quest_id):
        record = self.records.get(character_id, {}).get(quest_id)
        if record is None or record["status"] != "active":
            return {"progress": 0, "status": "not_active"}
        return {"progress": record["progress"], "status": "active"}

    def abandon(self, character_id, quest_id):
        char_records = self.records.setdefault(character_id, {})
        record = char_records.get(quest_id)
        if record is not None and record["status"] == "active":
            del char_records[quest_id]
            return {"abandoned": True}
        return {"abandoned": False}

    def turn_in(self, character_id, quest_id, now=0.0):
        if self.quests.get(quest_id, {}).get("daily"):
            period_index = int(now // self.period_seconds)
            key = (character_id, quest_id, period_index)
            if key in self.daily_counts:
                return {
                    "accepted": False,
                    "reason": "daily_limit",
                    "count": self.daily_counts[key],
                }
            self.daily_counts[key] = 1
            return {"accepted": True, "count": 1}
        return {"accepted": True, "count": 1}

    def log(self, character_id):
        return [
            {
                "quest_id": record["quest_id"],
                "progress": record["progress"],
                "status": record["status"],
                "source": record["source"],
            }
            for record in self.records.get(character_id, {}).values()
            if record["status"] == "active"
        ]

    def _ensure_record(self, character_id, quest_id, source):
        char_records = self.records.setdefault(character_id, {})
        record = char_records.get(quest_id)
        if record is None or record["status"] != "active":
            record = {
                "quest_id": quest_id,
                "progress": 0,
                "status": "active",
                "source": source,
            }
            char_records[quest_id] = record
        return record

    def _reject_reason(self, from_character, target_id, quest_id):
        sharer = self.characters[from_character]
        target = self.characters[target_id]
        if quest_id in target["completed"]:
            return "completed"
        record = self.records.get(target_id, {}).get(quest_id)
        if record is not None and record["status"] == "active":
            return "already_active"
        if target["faction"] != sharer["faction"]:
            return "faction"
        if target["phase"] != sharer["phase"]:
            return "phase"
        if target["map_id"] != sharer["map_id"]:
            return "map"
        distance = math.hypot(target["x"] - sharer["x"], target["y"] - sharer["y"])
        if distance > self.max_distance:
            return "distance"
        for prerequisite in self.quests.get(quest_id, {}).get("requires", []):
            if prerequisite not in target["completed"]:
                return "prerequisite"
        return None
