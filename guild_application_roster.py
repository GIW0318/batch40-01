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
        raise NotImplementedError

    def add_account(self, account_id, characters, guild=None):
        raise NotImplementedError

    def set_blacklist(self, account_id, blacklisted=True):
        raise NotImplementedError

    def apply(self, guild_id, player_id, account_id=None, note="", now=0.0):
        raise NotImplementedError

    def approve(self, guild_id, application_id, actor, now=0.0):
        raise NotImplementedError

    def reject(self, guild_id, application_id, actor, reason="", now=0.0):
        raise NotImplementedError

    def approve_batch(self, guild_id, application_ids, actor, now=0.0):
        raise NotImplementedError

    def capacity(self, guild_id):
        raise NotImplementedError

    def applications(self, guild_id):
        raise NotImplementedError

    def members(self, guild_id):
        raise NotImplementedError

    def transfer_leadership(self, guild_id, new_leader):
        raise NotImplementedError

    def expand(self, guild_id, new_capacity):
        raise NotImplementedError

    def events_for(self, guild_id):
        raise NotImplementedError
