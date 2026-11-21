"""Escort objective tracking with pause, checkpoint and settlement rules.

One escort instance exists per party and owns exactly one NPC.  Active time
only accumulates while the instance is running: out-of-range parties and
blocked NPCs pause the clock, disconnects get a grace window, and completion
and failure are mutually exclusive with a single settlement.
"""

import math


RUNNING_STATES = ("running", "paused")
TERMINAL_STATES = ("completed", "failed", "abandoned")


class EscortObjectiveTimer:
    def __init__(self, route=None, time_limit=300.0, pause_range=30.0, grace=30.0):
        self.route = dict(route or {})
        self.time_limit = float(self.route.get("time_limit", time_limit))
        self.pause_range = float(pause_range)
        self.grace = float(grace)
        self.npc_id = self.route.get("npc_id")
        self.checkpoints = list(self.route.get("checkpoints", []))
        self.positions = dict(self.route.get("positions", {}))
        self.instances = {}
        self._npc_owner = {}
        self._sequence = 0

    def start(self, party_id, members=None, now=0.0):
        existing = self.instances.get(party_id)
        if existing is not None and existing["state"] in RUNNING_STATES:
            return self._public_dict(existing)

        owner = self._npc_owner.get(self.npc_id)
        if owner is not None and owner != party_id:
            owner_instance = self.instances.get(owner)
            if owner_instance is not None and owner_instance["state"] in RUNNING_STATES:
                raise ValueError("escort npc is already bound to another party")

        if existing is not None:
            self._recycle(existing)

        self._sequence += 1
        member_list = list(members or [])
        instance = {
            "instance_id": "escort-%d" % self._sequence,
            "party_id": party_id,
            "npc_id": self.npc_id,
            "state": "running",
            "members": member_list,
            "disconnected": [],
            "disconnect_since": {},
            "checkpoint_index": 0,
            "checkpoints_done": [],
            "member_progress": {member: 0 for member in member_list},
            "elapsed": 0.0,
            "last_tick": float(now),
            "paused_reason": None,
            "npc_hp": 100.0,
            "reward_count": 0,
            "reason": None,
        }
        self.instances[party_id] = instance
        self._npc_owner[self.npc_id] = party_id
        return self._public_dict(instance)

    def advance(self, party_id, position, now=0.0, npc_position=None, members=None,
                npc_hp=None, blocked=False):
        instance = self._get_active(party_id)

        if members is not None:
            self._sync_members(instance, list(members), now)

        if npc_hp is not None:
            instance["npc_hp"] = float(npc_hp)
        if instance["npc_hp"] <= 0.0:
            return self._settle_fail(instance, "npc_dead", now)

        out_of_range = (
            npc_position is not None
            and self._distance(position, npc_position) > self.pause_range
        )
        should_pause = bool(blocked) or out_of_range
        pause_reason = "npc_blocked" if blocked else ("out_of_range" if out_of_range else None)

        if should_pause:
            if instance["state"] == "running":
                instance["state"] = "paused"
            instance["paused_reason"] = pause_reason
            instance["last_tick"] = float(now)
        else:
            if instance["state"] == "paused":
                instance["state"] = "running"
                instance["paused_reason"] = None
                instance["last_tick"] = float(now)
            else:
                instance["elapsed"] += max(0.0, float(now) - instance["last_tick"])
            instance["last_tick"] = float(now)

        if instance["elapsed"] > self.time_limit:
            return self._settle_fail(instance, "timeout", now)

        grace_fail = self._check_disconnect_grace(instance, now)
        if grace_fail is not None:
            return grace_fail

        return self._public_dict(instance)

    def checkpoint(self, party_id, checkpoint_id, now=0.0, position=None):
        instance = self._get_active(party_id)
        index = instance["checkpoint_index"]
        if index >= len(self.checkpoints) or self.checkpoints[index] != checkpoint_id:
            raise ValueError("checkpoints must be entered in order")
        instance["checkpoint_index"] = index + 1
        instance["checkpoints_done"].append(checkpoint_id)
        for member in instance["members"]:
            instance["member_progress"][member] = instance["checkpoint_index"]
        return self._public_dict(instance)

    def complete(self, party_id, now=0.0):
        instance = self.instances.get(party_id)
        if instance is None:
            raise ValueError("unknown escort instance")

        if instance["state"] == "completed":
            result = self._public_dict(instance)
            result["ok"] = False
            result["duplicate"] = True
            result["reward_issued"] = False
            return result
        if instance["state"] in TERMINAL_STATES:
            result = self._public_dict(instance)
            result["ok"] = False
            result["duplicate"] = False
            result["reward_issued"] = False
            return result

        if instance["checkpoint_index"] < len(self.checkpoints):
            return {
                "ok": False,
                "reason": "checkpoints_incomplete",
                "state": instance["state"],
            }

        instance["state"] = "completed"
        instance["paused_reason"] = None
        instance["reward_count"] = 1
        self._npc_owner.pop(instance["npc_id"], None)
        result = self._public_dict(instance)
        result["ok"] = True
        result["reward_issued"] = True
        return result

    def fail(self, party_id, reason, now=0.0):
        instance = self.instances.get(party_id)
        if instance is None:
            raise ValueError("unknown escort instance")
        if instance["state"] in TERMINAL_STATES:
            result = self._public_dict(instance)
            result["ok"] = False
            return result
        return self._settle_fail(instance, reason, now)

    def abandon(self, party_id, now=0.0):
        instance = self.instances.get(party_id)
        if instance is None:
            raise ValueError("unknown escort instance")
        instance["state"] = "abandoned"
        instance["paused_reason"] = None
        instance["reason"] = "abandoned"
        result = self._public_dict(instance)
        self._recycle(instance)
        return result

    def reconnect(self, party_id, member, now=0.0):
        instance = self._get_active(party_id)
        if member in instance["disconnected"]:
            instance["disconnected"].remove(member)
        instance["disconnect_since"].pop(member, None)
        if member not in instance["members"]:
            instance["members"].append(member)
            instance["member_progress"].setdefault(member, instance["checkpoint_index"])
        return self._public_dict(instance)

    def status(self, party_id):
        instance = self.instances.get(party_id)
        if instance is None:
            raise ValueError("unknown escort instance")
        return self._public_dict(instance)

    def snapshot(self, party_id):
        instance = self.instances.get(party_id)
        if instance is None:
            raise ValueError("unknown escort instance")
        return {
            "instance_id": instance["instance_id"],
            "party_id": instance["party_id"],
            "npc_id": instance["npc_id"],
            "state": instance["state"],
            "members": list(instance["members"]),
            "disconnected": list(instance["disconnected"]),
            "disconnect_since": dict(instance["disconnect_since"]),
            "checkpoint_index": instance["checkpoint_index"],
            "checkpoints_done": list(instance["checkpoints_done"]),
            "member_progress": dict(instance["member_progress"]),
            "elapsed": instance["elapsed"],
            "last_tick": instance["last_tick"],
            "paused_reason": instance["paused_reason"],
            "npc_hp": instance["npc_hp"],
            "reward_count": instance["reward_count"],
            "reason": instance["reason"],
        }

    def restore(self, snapshot, now=0.0):
        data = dict(snapshot)
        party_id = data["party_id"]
        data["members"] = list(data.get("members", []))
        data["disconnected"] = list(data.get("disconnected", []))
        data["disconnect_since"] = dict(data.get("disconnect_since", {}))
        data["checkpoints_done"] = list(data.get("checkpoints_done", []))
        data["member_progress"] = dict(data.get("member_progress", {}))
        data.setdefault("paused_reason", None)
        data.setdefault("reason", None)
        data["last_tick"] = float(now)
        self.instances[party_id] = data
        if data["state"] in RUNNING_STATES:
            self._npc_owner[data["npc_id"]] = party_id
        return self._public_dict(data)

    def _get_active(self, party_id):
        instance = self.instances.get(party_id)
        if instance is None:
            raise ValueError("unknown escort instance")
        if instance["state"] not in RUNNING_STATES:
            raise ValueError("escort instance is not active")
        return instance

    def _sync_members(self, instance, members, now):
        current = set(instance["members"])
        updated = set(members)

        for member in current - updated:
            if member not in instance["disconnected"]:
                instance["disconnected"].append(member)
                instance["disconnect_since"][member] = float(now)
        for member in updated - current:
            instance["members"].append(member)
            instance["member_progress"][member] = instance["checkpoint_index"]
            instance["disconnect_since"].pop(member, None)
            if member in instance["disconnected"]:
                instance["disconnected"].remove(member)
        instance["members"] = [
            member for member in instance["members"] if member in updated
        ] + [member for member in members if member not in current]

    def _check_disconnect_grace(self, instance, now):
        for member in list(instance["disconnected"]):
            since = instance["disconnect_since"].get(member, float(now))
            if float(now) - since > self.grace:
                return self._settle_fail(instance, "disconnect_timeout", now)
        return None

    def _settle_fail(self, instance, reason, now):
        if instance["state"] == "running":
            instance["elapsed"] += max(0.0, float(now) - instance["last_tick"])
        instance["last_tick"] = float(now)
        instance["state"] = "failed"
        instance["reason"] = reason
        instance["paused_reason"] = None
        self._npc_owner.pop(instance["npc_id"], None)
        result = self._public_dict(instance)
        result["ok"] = True
        return result

    def _recycle(self, instance):
        self._npc_owner.pop(instance["npc_id"], None)
        self.instances.pop(instance["party_id"], None)

    @staticmethod
    def _distance(left, right):
        return math.sqrt(
            (float(left[0]) - float(right[0])) ** 2
            + (float(left[1]) - float(right[1])) ** 2
        )

    def _public_dict(self, instance):
        return {
            "instance_id": instance["instance_id"],
            "party_id": instance["party_id"],
            "npc_id": instance["npc_id"],
            "state": instance["state"],
            "members": list(instance["members"]),
            "disconnected": list(instance["disconnected"]),
            "checkpoints_done": list(instance["checkpoints_done"]),
            "checkpoint_index": instance["checkpoint_index"],
            "member_progress": dict(instance["member_progress"]),
            "elapsed": instance["elapsed"],
            "remaining": max(0.0, self.time_limit - instance["elapsed"]),
            "reason": instance.get("paused_reason") or instance.get("reason"),
            "npc_hp": instance["npc_hp"],
            "reward_count": instance["reward_count"],
        }
