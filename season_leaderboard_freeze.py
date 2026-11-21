"""Season leaderboard submission, freeze, ranking and awards."""


class SeasonLeaderboardFreeze:
    def __init__(self, season_id="S1", season_end=1000.0, top_n=100, tiers=None):
        self.season_id = season_id
        self.season_end = season_end
        self.top_n = top_n
        self.tiers = list(tiers or [])
        self.entries = {}
        self.snapshots = {}
        self.version = 0
        self.frozen_at = None
        self.awarded = {}
        self.award_runs = {}
        self.appeals = []
        self.removals = []

    def submit(self, account, score, match_id=None, ended_at=0.0,
               submitted_at=0.0, server_id=None):
        raise NotImplementedError

    def merge_server(self, server_id, entries):
        raise NotImplementedError

    def freeze(self, now=None):
        raise NotImplementedError

    def rank(self, page=1, page_size=20, version=None):
        raise NotImplementedError

    def award(self, version=None):
        raise NotImplementedError

    def awarded_amount(self, account):
        raise NotImplementedError

    def appeal(self, account, new_score, case_id=None, now=0.0):
        raise NotImplementedError

    def remove(self, account, reason="violation", now=0.0):
        raise NotImplementedError
