"""Escort objective tracking with pause, checkpoint and settlement rules."""


class EscortObjectiveTimer:
    def __init__(self, route=None, time_limit=300.0, pause_range=30.0, grace=30.0):
        self.route = dict(route or {})
        self.time_limit = float(self.route.get("time_limit", time_limit))
        self.pause_range = float(pause_range)
        self.grace = float(grace)
        self.npc_id = self.route.get("npc_id")
        self.checkpoints = list(self.route.get("checkpoints", []))
        self.instances = {}

    def start(self, party_id, members=None, now=0.0):
        raise NotImplementedError

    def advance(self, party_id, position, now=0.0, npc_position=None, members=None,
                npc_hp=None, blocked=False):
        raise NotImplementedError

    def checkpoint(self, party_id, checkpoint_id, now=0.0, position=None):
        raise NotImplementedError

    def complete(self, party_id, now=0.0):
        raise NotImplementedError

    def fail(self, party_id, reason, now=0.0):
        raise NotImplementedError

    def abandon(self, party_id, now=0.0):
        raise NotImplementedError

    def reconnect(self, party_id, member, now=0.0):
        raise NotImplementedError

    def status(self, party_id):
        raise NotImplementedError

    def snapshot(self, party_id):
        raise NotImplementedError

    def restore(self, snapshot, now=0.0):
        raise NotImplementedError
