"""raid_lockout_ledger (initial skeleton)."""


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
        raise NotImplementedError()

    def enter(self, raid_id, difficulty, group_id, character_id, now):
        raise NotImplementedError()

    def record_kill(self, raid_id, difficulty, group_id, boss_id, confirmed, now, source_group=None):
        raise NotImplementedError()

    def extend(self, raid_id, difficulty, group_id, now):
        raise NotImplementedError()

    def reset(self, now):
        raise NotImplementedError()

    def query(self, raid_id, difficulty, group_id, now=None):
        raise NotImplementedError()

    def can_loot(self, raid_id, difficulty, group_id, boss_id, now=None):
        raise NotImplementedError()

    def transfer(self, character_id, from_group, to_group, raid_id, difficulty, now, fail=False):
        raise NotImplementedError()

    def audit(self):
        raise NotImplementedError()

    def export_state(self):
        raise NotImplementedError()

    def load_state(self, state):
        raise NotImplementedError()
