"""World drop ownership, protection window and claim locking."""


class WorlddropClaimWindow:
    def __init__(self, protection=60.0, damage_share=0.6):
        self.protection = protection
        self.damage_share = damage_share
        self.kills = {}
        self.drops = {}
        self.corpses = {}
        self.claims = {}
        self.quest_counts = {}
        self.request_results = {}
        self.player_factions = {}
        self.player_maps = {}
        self.mail = []

    def add_player(self, player, faction, map_id=None):
        raise NotImplementedError

    def register_kill(self, kill_id, mob_id, damage, last_hit_party, party_members,
                      factions, map_id, drops, now=0.0):
        raise NotImplementedError

    def rights(self, drop_id, player, now=0.0):
        raise NotImplementedError

    def claim(self, drop_id, player, now=0.0, request_id=None, client_now=None,
              reachable=True):
        raise NotImplementedError

    def expire(self, now=0.0):
        raise NotImplementedError

    def party_leave(self, party_id, member, now=0.0):
        raise NotImplementedError

    def teleport(self, player, map_id, now=0.0):
        raise NotImplementedError

    def corpse_state(self, corpse_id, now=0.0):
        raise NotImplementedError

    def mail_queue(self):
        raise NotImplementedError

    def snapshot(self, now=0.0):
        raise NotImplementedError

    def restore(self, snapshot, now=0.0):
        raise NotImplementedError
