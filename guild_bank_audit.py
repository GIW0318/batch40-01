"""Guild bank deposits, withdrawals, limits and audit trail."""


class GuildBankAudit:
    def __init__(self, guild_id="guild", capacity=20, max_limit=1000, timezone="+08:00"):
        self.guild_id = guild_id
        self.capacity = capacity
        self.max_limit = max_limit
        self.timezone = timezone
        self.slots = {}
        self.members = {}
        self.logs = []
        self.funds = 0
        self.requests = {}
        self.pages = 1
        self.dissolved = False
        self.mail = []
        self.asset_snapshots = []

    def add_member(self, actor, role="member", can_withdraw=True,
                   daily_limit=10, can_deposit=True):
        raise NotImplementedError

    def add_funds(self, amount):
        raise NotImplementedError

    def set_permission(self, actor, can_withdraw=None, role=None):
        raise NotImplementedError

    def open_panel(self, actor):
        raise NotImplementedError

    def deposit(self, actor, tab, page, quantity, item_id, bound_to=None,
                request_id=None, expected_version=None, now=0.0):
        raise NotImplementedError

    def withdraw(self, actor, tab, page, quantity, request_id=None,
                 expected_version=None, now=0.0):
        raise NotImplementedError

    def set_limit(self, actor, target, limit, request_id=None):
        raise NotImplementedError

    def move(self, actor, from_tab, from_page, to_tab, to_page, quantity,
             request_id=None, now=0.0):
        raise NotImplementedError

    def charge_repair(self, actor, amount, request_id=None, now=0.0):
        raise NotImplementedError

    def expand(self, actor, pages, now=0.0):
        raise NotImplementedError

    def log(self, tab=None, page=None):
        raise NotImplementedError

    def slot_state(self, tab, page):
        raise NotImplementedError

    def guild_funds(self):
        raise NotImplementedError

    def page_count(self):
        raise NotImplementedError

    def asset_snapshots_list(self):
        raise NotImplementedError

    def dissolve(self, actor, request_id=None, now=0.0):
        raise NotImplementedError

    def mail_queue(self):
        raise NotImplementedError
