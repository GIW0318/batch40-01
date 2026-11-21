"""crossrealm_match_pool (initial skeleton)."""


class CrossRealmMatchPool:
    """Cross-realm match pool.

    Players are dicts with id, role, rating and optional latency.  enqueue keeps a
    premade party intact, rejects duplicate parties and any player already queued
    in another region, and returns status queued with the party average rating.
    dequeue returns cancelled, matched or unknown and cannot cancel a formed
    match.  form_match pairs two parties that each have exactly team_size players,
    satisfy roles_required and are within rating_band, preferring the oldest
    queued_at; both are removed atomically and the match record has match_id,
    party_ids, teams, ratings, roles, status, winner, created_at and open_slots.
    backfill only adds to an active underfilled match when the chosen team still
    has that role gap; statuses are added, ended, role_full and duplicate, and an
    added result includes team and open_slots.
    end_match marks a match ended.  probe adds latency samples and latency_for
    returns the median of the last three samples.  ping refreshes one player;
    tick(now) expires queued parties and removes match players whose last_seen is
    older than offline_timeout, updating open_slots.  match_record returns a deep
    copy.  status returns queued, matches and players counts.  A duplicate enqueue
    includes player_id, dequeue of a matched party includes match_id, and tick
    returns expired_parties and dropped_players.
    """

    def __init__(self, team_size=3, roles_required=None, rating_band=100, offline_timeout=30.0):
        raise NotImplementedError()

    def enqueue(self, party_id, players, region, now=0.0):
        raise NotImplementedError()

    def dequeue(self, party_id):
        raise NotImplementedError()

    def form_match(self, now=0.0):
        raise NotImplementedError()

    def backfill(self, match_id, player, role, now=0.0):
        raise NotImplementedError()

    def end_match(self, match_id, winner=None):
        raise NotImplementedError()

    def ping(self, player_id, now):
        raise NotImplementedError()

    def tick(self, now):
        raise NotImplementedError()

    def probe(self, player_id, latency):
        raise NotImplementedError()

    def latency_for(self, player_id):
        raise NotImplementedError()

    def status(self):
        raise NotImplementedError()

    def match_record(self, match_id):
        raise NotImplementedError()
