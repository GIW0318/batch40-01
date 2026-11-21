"""Guild contribution accrual, settlement, redemption and ledger."""


class GuildContributionLedger:
    def __init__(self, period_seconds=7 * 86400.0, period_offset=12 * 3600.0,
                 wage_rate=1.0, compensation_cap=500):
        self.period_seconds = period_seconds
        self.period_offset = period_offset
        self.wage_rate = wage_rate
        self.compensation_cap = compensation_cap
        self.guilds = {}
        self.members = {}
        self.activities = {}
        self.ledger_entries = []
        self.requests = {}

    def add_guild(self, guild_id, funds=0, period_offset=None, wage_rate=None,
                  compensation_cap=None):
        raise NotImplementedError

    def add_funds(self, guild_id, amount):
        raise NotImplementedError

    def add_member(self, guild_id, member, joined_at=0.0, active=True, role="member"):
        raise NotImplementedError

    def accrue(self, guild_id, activity_id, member, amount, now=0.0, source="activity"):
        raise NotImplementedError

    def settle(self, guild_id, period=None, now=0.0):
        raise NotImplementedError

    def redeem(self, guild_id, member, cost, item_id, request_id=None, now=0.0):
        raise NotImplementedError

    def compensate(self, guild_id, actor, member, amount, reason,
                   request_id=None, now=0.0):
        raise NotImplementedError

    def leave(self, guild_id, member):
        raise NotImplementedError

    def join(self, guild_id, member, now=0.0):
        raise NotImplementedError

    def balance(self, guild_id, member):
        raise NotImplementedError

    def guild_funds(self, guild_id):
        raise NotImplementedError

    def ledger(self, guild_id=None, period=None, public=False):
        raise NotImplementedError

    def leaderboard(self, guild_id, period=None):
        raise NotImplementedError
