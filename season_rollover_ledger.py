"""Season rollover roster, reset, reward grant and claims."""


class SeasonRolloverLedger:
    def __init__(self, season_id="S1", reset_rules=None, batch_size=2,
                 mail_ttl=7 * 86400.0, rewards=None):
        self.season_id = season_id
        self.reset_rules = dict(reset_rules or {})
        self.batch_size = batch_size
        self.mail_ttl = mail_ttl
        self.rewards = dict(rewards or {})
        self.players = {}
        self.roster = None
        self.roster_version = 0
        self.checkpoint = None
        self.reset_done = set()
        self.pending_pool = set()
        self.grant_results = {}
        self.claims = set()

    def load_players(self, players):
        raise NotImplementedError

    def add_player(self, account, rank="bronze", peak=None, placement_done=True,
                   banned=False, streak=0, tokens=0):
        raise NotImplementedError

    def update_player(self, account, **fields):
        raise NotImplementedError

    def get_player(self, account):
        raise NotImplementedError

    def player_accounts(self):
        raise NotImplementedError

    def pending_accounts(self):
        raise NotImplementedError

    def build_roster(self, now=0.0):
        raise NotImplementedError

    def reset(self, season_id=None, fail_after=None):
        raise NotImplementedError

    def grant(self, season_id=None, now=0.0):
        raise NotImplementedError

    def claim(self, account, tier, season_id=None, now=0.0):
        raise NotImplementedError

    def reclaim_tokens(self, season_id=None):
        raise NotImplementedError
