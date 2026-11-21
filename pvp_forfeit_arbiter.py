"""PvP forfeit arbiter.

Settles arena forfeit votes and disconnect timeouts.  All mutating operations
are serialized with a single lock so result writes and rematch requests can
never interleave, and every match keeps one shared, strictly increasing event
sequence for votes, disconnects, reconnects, finalization and rematches.
"""

import threading


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

    def __init__(self, vote_threshold=0.5, grace=30.0, cooldown=5.0,
                 points_win=3, points_loss=0):
        self.vote_threshold = vote_threshold
        self.grace = grace
        self.cooldown = cooldown
        self.points_win = points_win
        self.points_loss = points_loss
        self._matches = {}
        self._lock = threading.RLock()

    def create_match(self, match_id, teams):
        with self._lock:
            player_team = {}
            for team, players in teams.items():
                for player in players:
                    player_team[player] = team
            self._matches[match_id] = {
                "match_id": match_id,
                "teams": {team: list(players) for team, players in teams.items()},
                "player_team": player_team,
                "status": "active",
                "winner": None,
                "reason": None,
                "points": {team: 0 for team in teams},
                "events": [],
                "seq": 0,
                "votes": set(),
                "last_vote_at": {},
                "disconnected": {},
                "rematch_scheduled": False,
            }
            return {"match_id": match_id, "status": "active"}

    def _append_event(self, match, kind, now, **details):
        match["seq"] += 1
        event = {"seq": match["seq"], "kind": kind, "at": now}
        event.update(details)
        match["events"].append(event)
        return event

    def _required_votes(self, match, team):
        eligible = [
            player for player in match["teams"][team]
            if player not in match["disconnected"]
        ]
        required = int(len(eligible) * self.vote_threshold) + 1
        return required, eligible

    def _settle(self, match, winner, reason, now):
        """Write the single settlement record for a match."""
        if match["status"] == "finished":
            return
        match["status"] = "finished"
        match["winner"] = winner
        match["reason"] = reason
        if winner is None:
            for team in match["teams"]:
                match["points"][team] = 0
        else:
            for team in match["teams"]:
                match["points"][team] = (
                    self.points_win if team == winner else self.points_loss
                )
        self._append_event(
            match, "finalize", now, winner=winner, reason=reason
        )

    def vote_forfeit(self, match_id, team, player, now=0.0):
        with self._lock:
            match = self._matches.get(match_id)
            if match is None:
                return {"status": "not_in_match"}
            if match["status"] == "finished":
                return {"status": "finished"}
            if match["player_team"].get(player) != team:
                return {"status": "not_in_match"}
            if player in match["disconnected"]:
                return {"status": "not_in_match"}

            retry_at = match["last_vote_at"].get(player)
            if retry_at is not None:
                retry_at += self.cooldown
                if now < retry_at:
                    return {"status": "cooldown", "retry_at": retry_at}

            match["last_vote_at"][player] = now
            match["votes"].add(player)
            self._append_event(match, "vote", now, team=team, player=player)

            required, eligible = self._required_votes(match, team)
            current = len(match["votes"].intersection(eligible))
            if current >= required:
                opponent = next(other for other in match["teams"] if other != team)
                self._settle(match, opponent, "forfeit", now)
                return {
                    "status": "passed",
                    "winner": opponent,
                    "reason": "forfeit",
                    "required": required,
                }
            return {"status": "counted", "required": required}

    def disconnect(self, match_id, player, now=0.0):
        with self._lock:
            match = self._matches.get(match_id)
            if match is None or player not in match["player_team"]:
                return {"status": "not_in_match"}
            if match["status"] == "finished":
                return {"status": "finished"}

            grace_until = now + self.grace
            match["disconnected"][player] = grace_until
            self._append_event(
                match, "disconnect", now,
                player=player, team=match["player_team"][player],
                grace_until=grace_until,
            )
            return {"status": "grace", "grace_until": grace_until}

    def reconnect(self, match_id, player, now=0.0):
        with self._lock:
            match = self._matches.get(match_id)
            if match is None or player not in match["player_team"]:
                return {"status": "not_in_match"}
            if match["status"] == "finished":
                return {"status": "finished"}

            grace_until = match["disconnected"].get(player)
            if grace_until is None:
                return {"status": "restored"}
            if now < grace_until:
                del match["disconnected"][player]
                self._append_event(
                    match, "reconnect", now,
                    player=player, team=match["player_team"][player],
                )
                return {"status": "restored", "grace_until": grace_until}

            team = match["player_team"][player]
            opponent = next(other for other in match["teams"] if other != team)
            self._settle(match, opponent, "disconnect", now)
            return {"status": "expired", "reason": "disconnect"}

    def tick(self, now):
        with self._lock:
            finalized = []
            for match in self._matches.values():
                if match["status"] == "finished":
                    continue
                for player, grace_until in list(match["disconnected"].items()):
                    if now >= grace_until:
                        team = match["player_team"][player]
                        opponent = next(
                            other for other in match["teams"] if other != team
                        )
                        self._settle(match, opponent, "disconnect", now)
                        finalized.append(match["match_id"])
                        break
            return {"finalized": finalized}

    def finalize(self, match_id, winner=None, reason=None, now=0.0):
        with self._lock:
            match = self._matches.get(match_id)
            if match is None:
                return {"finalized": False, "reason": "not found"}
            if match["status"] == "finished":
                return {"finalized": False, "reason": "already finished"}

            if reason is None:
                reason = "draw" if winner is None else "finalized"
            self._settle(match, winner, reason, now)
            return {
                "finalized": True,
                "winner": winner,
                "reason": reason,
            }

    def request_rematch(self, match_id, now=0.0):
        with self._lock:
            match = self._matches.get(match_id)
            if match is None or match["status"] != "finished":
                return {"status": "not_finished"}
            if match["rematch_scheduled"]:
                return {"status": "already_requested"}
            match["rematch_scheduled"] = True
            self._append_event(match, "rematch", now)
            return {"status": "scheduled"}

    def record(self, match_id):
        with self._lock:
            match = self._matches.get(match_id)
            if match is None:
                return None
            return {
                "match_id": match["match_id"],
                "status": match["status"],
                "winner": match["winner"],
                "reason": match["reason"],
                "points": dict(match["points"]),
                "teams": {
                    team: list(players)
                    for team, players in match["teams"].items()
                },
                "events": [dict(event) for event in match["events"]],
                "rematch_scheduled": match["rematch_scheduled"],
            }

    def replay(self, match_id):
        with self._lock:
            record = self.record(match_id)
            if record is None:
                return None
            return {
                "winner": record["winner"],
                "reason": record["reason"],
                "points": record["points"],
                "teams": record["teams"],
            }
