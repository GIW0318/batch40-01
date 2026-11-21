"""Personal quest sharing across party members."""


class QuestShareRelay:
    def __init__(self, quests=None, max_distance=50.0, request_ttl=60.0, period_seconds=86400.0):
        self.quests = dict(quests or {})
        self.max_distance = max_distance
        self.request_ttl = request_ttl
        self.period_seconds = period_seconds
        self.characters = {}
        self.parties = {}
        self.requests = {}

    def add_character(self, character_id, faction="neutral", phase=1, map_id="m1",
                      x=0.0, y=0.0, completed=None):
        raise NotImplementedError

    def add_party(self, party_id, leader, members=None):
        raise NotImplementedError

    def set_leader(self, party_id, leader):
        raise NotImplementedError

    def disband(self, party_id):
        raise NotImplementedError

    def share(self, request_id, party_id, from_character, quest_id, now=0.0):
        raise NotImplementedError

    def accept(self, request_id, character_id, now=0.0):
        raise NotImplementedError

    def add_progress(self, character_id, quest_id, amount, now=0.0):
        raise NotImplementedError

    def progress(self, character_id, quest_id):
        raise NotImplementedError

    def abandon(self, character_id, quest_id):
        raise NotImplementedError

    def turn_in(self, character_id, quest_id, now=0.0):
        raise NotImplementedError

    def log(self, character_id):
        raise NotImplementedError
