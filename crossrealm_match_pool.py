"""crossrealm_match_pool."""

import copy
import statistics
import threading


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
        self.team_size = team_size
        if roles_required is None:
            roles_required = {"tank": 1, "healer": 1, "dps": max(team_size - 2, 0)}
        self.roles_required = dict(roles_required)
        self.rating_band = rating_band
        self.offline_timeout = offline_timeout
        self._lock = threading.RLock()
        self._parties = {}
        self._player_party = {}
        self._matches = {}
        self._match_roles = {}
        self._player_match = {}
        self._party_match = {}
        self._latency = {}
        self._last_seen = {}
        self._match_seq = 0

    def enqueue(self, party_id, players, region, now=0.0):
        with self._lock:
            if party_id in self._parties or party_id in self._party_match:
                return {"status": "duplicate", "party_id": party_id}
            for player in players:
                player_id = player["id"]
                if player_id in self._player_party or player_id in self._player_match:
                    return {"status": "duplicate", "player_id": player_id}
            ratings = [player.get("rating", 0) for player in players]
            average = sum(ratings) / float(len(ratings)) if ratings else 0.0
            self._parties[party_id] = {
                "party_id": party_id,
                "players": [dict(player) for player in players],
                "region": region,
                "rating": average,
                "queued_at": now,
            }
            for player in players:
                player_id = player["id"]
                self._player_party[player_id] = party_id
                self._last_seen[player_id] = now
                samples = list(player.get("latency") or [])
                if samples:
                    self._latency[player_id] = samples[-3:]
            return {"status": "queued", "party_id": party_id, "rating": average}

    def dequeue(self, party_id):
        with self._lock:
            if party_id in self._parties:
                self._remove_party(party_id)
                return {"status": "cancelled", "party_id": party_id}
            if party_id in self._party_match:
                return {
                    "status": "matched",
                    "party_id": party_id,
                    "match_id": self._party_match[party_id],
                }
            return {"status": "unknown", "party_id": party_id}

    def form_match(self, now=0.0):
        with self._lock:
            eligible = [
                party
                for party in self._parties_by_wait()
                if self._is_eligible(party)
            ]
            for first_index in range(len(eligible)):
                for second_index in range(first_index + 1, len(eligible)):
                    first = eligible[first_index]
                    second = eligible[second_index]
                    if abs(first["rating"] - second["rating"]) <= self.rating_band:
                        return self._create_match(first, second, now)
            return None

    def backfill(self, match_id, player, role, now=0.0):
        with self._lock:
            match = self._matches.get(match_id)
            if match is None:
                return {"status": "unknown", "match_id": match_id}
            if match["status"] != "active":
                return {"status": "ended", "match_id": match_id}
            player_id = player["id"]
            if player_id in self._player_match or player_id in self._player_party:
                return {"status": "duplicate", "player_id": player_id}
            roles = self._match_roles[match_id]
            target = None
            for team_index, team in enumerate(match["teams"]):
                if len(team) >= self.team_size:
                    continue
                counts = {}
                for member in team:
                    member_role = roles.get(member)
                    counts[member_role] = counts.get(member_role, 0) + 1
                if counts.get(role, 0) >= self.roles_required.get(role, 0):
                    continue
                if target is None or len(team) < len(match["teams"][target]):
                    target = team_index
            if target is None:
                return {"status": "role_full", "match_id": match_id}
            match["teams"][target].append(player_id)
            match["roles"][target].append(role)
            roles[player_id] = role
            match["open_slots"] = self._open_slots(match)
            self._player_match[player_id] = match_id
            self._last_seen[player_id] = now
            samples = list(player.get("latency") or [])
            if samples:
                self._latency[player_id] = samples[-3:]
            return {
                "status": "added",
                "match_id": match_id,
                "team": target,
                "open_slots": match["open_slots"],
            }

    def end_match(self, match_id, winner=None):
        with self._lock:
            match = self._matches.get(match_id)
            if match is None:
                return {"status": "unknown", "match_id": match_id}
            match["status"] = "ended"
            match["winner"] = winner
            for player_id, owner in list(self._player_match.items()):
                if owner == match_id:
                    del self._player_match[player_id]
            for party_id in match["party_ids"]:
                self._party_match.pop(party_id, None)
            return {"status": "ended", "match_id": match_id, "winner": winner}

    def ping(self, player_id, now):
        with self._lock:
            self._last_seen[player_id] = now
            return {"status": "ok", "player_id": player_id}

    def tick(self, now):
        with self._lock:
            expired_parties = []
            for party_id, party in list(self._parties.items()):
                last_seen = party["queued_at"]
                for player in party["players"]:
                    last_seen = max(
                        last_seen, self._last_seen.get(player["id"], party["queued_at"])
                    )
                if now - last_seen > self.offline_timeout:
                    expired_parties.append(party_id)
                    self._remove_party(party_id)
            dropped_players = []
            for match_id, match in self._matches.items():
                if match["status"] != "active":
                    continue
                roles = self._match_roles[match_id]
                for team_index, team in enumerate(match["teams"]):
                    for player_id in list(team):
                        seen = self._last_seen.get(player_id, match["created_at"])
                        if now - seen > self.offline_timeout:
                            team.remove(player_id)
                            role = roles.pop(player_id, None)
                            if role in match["roles"][team_index]:
                                match["roles"][team_index].remove(role)
                            self._player_match.pop(player_id, None)
                            dropped_players.append(player_id)
                match["open_slots"] = self._open_slots(match)
            return {
                "expired_parties": expired_parties,
                "dropped_players": dropped_players,
            }

    def probe(self, player_id, latency):
        with self._lock:
            samples = self._latency.setdefault(player_id, [])
            samples.append(latency)
            del samples[:-3]

    def latency_for(self, player_id):
        with self._lock:
            samples = self._latency.get(player_id)
            if not samples:
                return None
            return statistics.median(samples)

    def status(self):
        with self._lock:
            return {
                "queued": len(self._parties),
                "matches": sum(
                    1 for match in self._matches.values() if match["status"] == "active"
                ),
                "players": sum(len(party["players"]) for party in self._parties.values()),
            }

    def match_record(self, match_id):
        with self._lock:
            match = self._matches.get(match_id)
            if match is None:
                return None
            return copy.deepcopy(match)

    def _parties_by_wait(self):
        return sorted(
            self._parties.values(),
            key=lambda party: (party["queued_at"], party["party_id"]),
        )

    def _is_eligible(self, party):
        if len(party["players"]) != self.team_size:
            return False
        counts = {}
        for player in party["players"]:
            role = player.get("role")
            counts[role] = counts.get(role, 0) + 1
        for role, required in self.roles_required.items():
            if counts.get(role, 0) < required:
                return False
        return True

    def _create_match(self, first, second, now):
        self._match_seq += 1
        match_id = "M%d" % self._match_seq
        match = {
            "match_id": match_id,
            "party_ids": [first["party_id"], second["party_id"]],
            "teams": [
                [player["id"] for player in first["players"]],
                [player["id"] for player in second["players"]],
            ],
            "ratings": [first["rating"], second["rating"]],
            "roles": [
                [player.get("role") for player in first["players"]],
                [player.get("role") for player in second["players"]],
            ],
            "status": "active",
            "winner": None,
            "created_at": now,
            "open_slots": 0,
        }
        self._matches[match_id] = match
        roles = {}
        for party in (first, second):
            for player in party["players"]:
                roles[player["id"]] = player.get("role")
        self._match_roles[match_id] = roles
        for party in (first, second):
            self._remove_party(party["party_id"])
            self._party_match[party["party_id"]] = match_id
            for player in party["players"]:
                player_id = player["id"]
                self._player_match[player_id] = match_id
                self._last_seen[player_id] = now
        return copy.deepcopy(match)

    def _remove_party(self, party_id):
        party = self._parties.pop(party_id, None)
        if party is not None:
            for player in party["players"]:
                self._player_party.pop(player["id"], None)

    def _open_slots(self, match):
        filled = sum(len(team) for team in match["teams"])
        return 2 * self.team_size - filled
