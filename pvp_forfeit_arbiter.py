"""pvp_forfeit_arbiter (initial skeleton)."""


class PvpForfeitArbiter:
    """PvP forfeit arbiter.

    create_match(match_id, teams) takes {"A": [...], "B": [...]}.  Votes count per
    team and only connected members are eligible, so disconnected players abstain
    from the denominator.  required is floor(len(eligible) * vote_threshold) + 1;
    when reached the other team wins with reason forfeit.  A player's vote has a
    cooldown per match.  disconnect puts a player in grace until now + grace;
    reconnect before that instant restores them, otherwise the opposing team wins
    with reason disconnect, and tick(now) applies the same timeout.  finalize
    accepts winner A, B or None and succeeds only once; afterwards
    vote/disconnect/reconnect return status finished.  points are points_win for
    the winner, points_loss for the loser and 0 for both on a draw.  record
    returns match_id, status, winner, reason, points, teams, events and
    rematch_scheduled; every event has an increasing unique seq.  replay returns
    winner, reason, points and teams from the same record.  request_rematch
    succeeds once after finalization; its statuses are not_finished, scheduled and
    already_requested.  Vote statuses are counted, passed,
    cooldown (with retry_at), finished and not_in_match; disconnect returns grace
    with grace_until; reconnect returns restored or expired; a repeated finalize
    reports reason 'already finished'; tick returns finalized ids.
    """

    def __init__(self, vote_threshold=0.5, grace=30.0, cooldown=5.0, points_win=3, points_loss=0):
        raise NotImplementedError()

    def create_match(self, match_id, teams):
        raise NotImplementedError()

    def vote_forfeit(self, match_id, team, player, now=0.0):
        raise NotImplementedError()

    def disconnect(self, match_id, player, now=0.0):
        raise NotImplementedError()

    def reconnect(self, match_id, player, now=0.0):
        raise NotImplementedError()

    def tick(self, now):
        raise NotImplementedError()

    def finalize(self, match_id, winner=None, reason=None, now=0.0):
        raise NotImplementedError()

    def request_rematch(self, match_id, now=0.0):
        raise NotImplementedError()

    def record(self, match_id):
        raise NotImplementedError()

    def replay(self, match_id):
        raise NotImplementedError()
