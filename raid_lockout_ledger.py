"""raid_lockout_ledger."""

import copy
import math


class RaidLockoutLedger:
    """Raid lockout ledger.

    A period is floor(now / period_seconds).  enter returns the lockout for
    (raid_id, difficulty, period, group_id) with lockout_id, raid_id, difficulty,
    period, group_id, expires_at, members and bosses; expires_at is
    (period + 1) * period_seconds.  Different groups and difficulties have
    separate records and a group never sees another group's bosses.  record_kill
    only appends when confirmed is true, ignores duplicate boss ids, and stores
    boss_id, killed_at and source_group (default group_id); it returns recorded and
    reason.  extend targets the newest matching lockout, refuses when
    now >= expires_at, moves expiry to (period + 2) * period_seconds and appends an
    audit entry containing action, lockout_id, at, old_expires_at and
    new_expires_at.  reset(now) removes lockouts whose expires_at <= now and
    returns their ids.  query takes an optional now and returns a deep copy or
    None.  can_loot is false once the boss is recorded.  transfer moves one
    character between groups atomically; fail=True leaves state untouched and the
    destination group must not inherit bosses.  export_state/load_state round-trip
    lockouts and the audit log.  record_kill reasons are 'not confirmed' and
    'duplicate'; extend reasons are 'expired' and 'no lockout'.
    """

    def __init__(self, period_seconds=100.0):
        self.period_seconds = period_seconds
        self._lockouts = {}
        self._audit = []

    def _period_of(self, now):
        return math.floor(now / self.period_seconds)

    def _key(self, raid_id, difficulty, period, group_id):
        return (raid_id, difficulty, period, group_id)

    @staticmethod
    def _lockout_id(raid_id, difficulty, period, group_id):
        return f"{raid_id}:{difficulty}:{period}:{group_id}"

    def _new_lockout(self, raid_id, difficulty, period, group_id):
        return {
            "lockout_id": self._lockout_id(raid_id, difficulty, period, group_id),
            "raid_id": raid_id,
            "difficulty": difficulty,
            "period": period,
            "group_id": group_id,
            "expires_at": (period + 1) * self.period_seconds,
            "members": [],
            "bosses": [],
        }

    def _newest_matching(self, raid_id, difficulty, group_id):
        matches = [
            lockout
            for lockout in self._lockouts.values()
            if lockout["raid_id"] == raid_id
            and lockout["difficulty"] == difficulty
            and lockout["group_id"] == group_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda lockout: lockout["period"])

    def enter(self, raid_id, difficulty, group_id, character_id, now):
        period = self._period_of(now)
        key = self._key(raid_id, difficulty, period, group_id)
        lockout = self._lockouts.get(key)
        if lockout is None:
            lockout = self._new_lockout(raid_id, difficulty, period, group_id)
            self._lockouts[key] = lockout
        if character_id is not None and character_id not in lockout["members"]:
            lockout["members"].append(character_id)
        return copy.deepcopy(lockout)

    def record_kill(self, raid_id, difficulty, group_id, boss_id, confirmed, now, source_group=None):
        if not confirmed:
            return {"recorded": False, "reason": "not confirmed"}
        period = self._period_of(now)
        lockout = self._lockouts.get(
            self._key(raid_id, difficulty, period, group_id)
        )
        if lockout is None:
            return {"recorded": False, "reason": "no lockout"}
        if any(boss["boss_id"] == boss_id for boss in lockout["bosses"]):
            return {"recorded": False, "reason": "duplicate"}
        lockout["bosses"].append(
            {
                "boss_id": boss_id,
                "killed_at": now,
                "source_group": source_group if source_group is not None else group_id,
            }
        )
        return {"recorded": True, "reason": None}

    def extend(self, raid_id, difficulty, group_id, now):
        lockout = self._newest_matching(raid_id, difficulty, group_id)
        if lockout is None:
            return {"extended": False, "reason": "no lockout"}
        if now >= lockout["expires_at"]:
            return {"extended": False, "reason": "expired"}
        old_expires_at = lockout["expires_at"]
        new_expires_at = (lockout["period"] + 2) * self.period_seconds
        lockout["expires_at"] = new_expires_at
        self._audit.append(
            {
                "action": "extend",
                "lockout_id": lockout["lockout_id"],
                "at": now,
                "old_expires_at": old_expires_at,
                "new_expires_at": new_expires_at,
            }
        )
        return {"extended": True, "reason": None, "expires_at": new_expires_at}

    def reset(self, now):
        expired = [
            key
            for key, lockout in self._lockouts.items()
            if lockout["expires_at"] <= now
        ]
        reset_ids = [self._lockouts[key]["lockout_id"] for key in expired]
        for key in expired:
            del self._lockouts[key]
        return reset_ids

    def query(self, raid_id, difficulty, group_id, now=None):
        lockout = self._newest_matching(raid_id, difficulty, group_id)
        if lockout is None:
            return None
        if now is not None and now >= lockout["expires_at"]:
            return None
        return copy.deepcopy(lockout)

    def can_loot(self, raid_id, difficulty, group_id, boss_id, now=None):
        lockout = self._newest_matching(raid_id, difficulty, group_id)
        if lockout is None:
            return True
        if now is not None and now >= lockout["expires_at"]:
            return True
        return not any(boss["boss_id"] == boss_id for boss in lockout["bosses"])

    def transfer(self, character_id, from_group, to_group, raid_id, difficulty, now, fail=False):
        if fail:
            return {"transferred": False, "reason": "failed"}
        period = self._period_of(now)
        source_key = self._key(raid_id, difficulty, period, from_group)
        source = self._lockouts.get(source_key)
        if source is None or character_id not in source["members"]:
            return {"transferred": False, "reason": "no lockout"}

        snapshot = copy.deepcopy(self._lockouts)
        try:
            source["members"].remove(character_id)
            target_key = self._key(raid_id, difficulty, period, to_group)
            target = self._lockouts.get(target_key)
            if target is None:
                target = self._new_lockout(raid_id, difficulty, period, to_group)
                self._lockouts[target_key] = target
            if character_id not in target["members"]:
                target["members"].append(character_id)
        except Exception:
            self._lockouts = snapshot
            raise
        return {"transferred": True, "reason": None}

    def audit(self):
        return copy.deepcopy(self._audit)

    def export_state(self):
        return {
            "period_seconds": self.period_seconds,
            "lockouts": copy.deepcopy(list(self._lockouts.values())),
            "audit": copy.deepcopy(self._audit),
        }

    def load_state(self, state):
        self.period_seconds = state["period_seconds"]
        self._lockouts = {}
        for lockout in state["lockouts"]:
            lockout_copy = copy.deepcopy(lockout)
            key = self._key(
                lockout_copy["raid_id"],
                lockout_copy["difficulty"],
                lockout_copy["period"],
                lockout_copy["group_id"],
            )
            self._lockouts[key] = lockout_copy
        self._audit = copy.deepcopy(state["audit"])
        return None
