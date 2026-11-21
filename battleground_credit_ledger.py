"""battleground_credit_ledger."""

from copy import deepcopy


class BattlegroundCreditLedger:
    """Battleground honor credit ledger.

    Players are registered with add_player(player_id, team, pet_of=None); a pet's
    team follows its owner.  Every event carries a globally unique event_seq and a
    repeated sequence is ignored.  record_damage returns duplicate, effective,
    overkill, credited_to and credited; effective damage is capped by hp_before
    and the excess is overkill.  record_heal returns effective and overheal,
    capped by max_hp - hp_before.  Pet damage and pet kills are credited to the
    owner.  Kill credit goes to the killing blow, not the top damage dealer;
    assists are players who dealt effective damage or healing to the victim in
    [event_time - assist_window, event_time], ordered by contribution.  record_kill
    returns duplicate, killer, victim, assists and credited.  respawn clears the
    victim's old contributions and kill lock.  leave and replace_player truncate
    credit at the leave time.  standings rows are player_id, team, kills, assists,
    honor, left, kicked, sorted by honor desc then player_id; honor is
    3 * kills + assists.  settle(winner_team) freezes the ledger, adds
    VICTORY_HONOR to present winning-team members but not to kicked or left
    players, and returns frozen, winner_team, standings and mails; exactly one
    merged mail per non-kicked player with player_id, kills, assists, honor and
    parts=1.  Repeated settle returns the same result.  credits_for_player and
    credits_for_event return copies sorted by event time.  Events received after
    settle return ignored='settled' and duplicate events return duplicate=True.
    """
    VICTORY_HONOR = 2
    HONOR_PER_KILL = 3
    HONOR_PER_ASSIST = 1

    def __init__(self, assist_window=8.0):
        self.assist_window = assist_window
        self._players = {}
        self._seen_events = set()
        self._event_credits = {}
        self._player_credits = {}
        self._contributions = {}
        self._dead_victims = set()
        self._last_assists = {}
        self._kills = {}
        self._assist_counts = {}
        self._victory_honor = {}
        self._settled = False
        self._settle_result = None

    def add_player(self, player_id, team, pet_of=None):
        self._players[player_id] = {
            "team": team,
            "pet_of": pet_of,
            "is_pet": pet_of is not None,
            "left_at": None,
            "kicked": False,
        }
        if pet_of is None:
            self._player_credits.setdefault(player_id, [])
            self._kills.setdefault(player_id, 0)
            self._assist_counts.setdefault(player_id, 0)

    def leave(self, player_id, at, kicked=False):
        player = self._players.get(player_id)
        if player is None:
            return
        player["left_at"] = at
        player["kicked"] = kicked

    def replace_player(self, old_id, new_id, at):
        old_player = self._players.get(old_id)
        if old_player is None:
            return
        old_player["left_at"] = at
        self.add_player(new_id, old_player["team"])

    def respawn(self, victim_id, at):
        self._contributions.pop(victim_id, None)
        self._dead_victims.discard(victim_id)
        self._last_assists.pop(victim_id, None)

    def record_damage(self, event_seq, source_id, target_id, amount, hp_before, event_time):
        if self._settled:
            return {
                "ignored": "settled",
                "duplicate": False,
                "credited": False,
                "credited_to": None,
                "effective": 0,
                "overkill": 0,
            }
        if event_seq in self._seen_events:
            return {
                "duplicate": True,
                "credited": False,
                "credited_to": None,
                "effective": 0,
                "overkill": 0,
            }
        self._seen_events.add(event_seq)

        owner_id = self._resolve_owner(source_id)
        effective = max(0, min(amount, max(0, hp_before)))
        overkill = max(0, amount - effective)
        credited = self._is_present(owner_id, event_time)

        if credited and effective > 0:
            record = {
                "event_seq": event_seq,
                "kind": "damage",
                "source_id": source_id,
                "credited_to": owner_id,
                "target_id": target_id,
                "amount": effective,
                "overkill": overkill,
                "event_time": event_time,
                "credited": True,
            }
            self._store_credit(owner_id, event_seq, record)
            self._add_contribution(target_id, owner_id, effective, event_time)

        return {
            "duplicate": False,
            "credited": credited,
            "credited_to": owner_id,
            "effective": effective,
            "overkill": overkill,
        }

    def record_heal(self, event_seq, source_id, target_id, amount, hp_before, max_hp, event_time):
        if self._settled:
            return {
                "ignored": "settled",
                "duplicate": False,
                "credited": False,
                "credited_to": None,
                "effective": 0,
                "overheal": 0,
            }
        if event_seq in self._seen_events:
            return {
                "duplicate": True,
                "credited": False,
                "credited_to": None,
                "effective": 0,
                "overheal": 0,
            }
        self._seen_events.add(event_seq)

        owner_id = self._resolve_owner(source_id)
        room = max(0, max_hp - hp_before)
        effective = max(0, min(amount, room))
        overheal = max(0, amount - effective)
        credited = self._is_present(owner_id, event_time)

        if credited and effective > 0:
            record = {
                "event_seq": event_seq,
                "kind": "heal",
                "source_id": source_id,
                "credited_to": owner_id,
                "target_id": target_id,
                "amount": effective,
                "overheal": overheal,
                "event_time": event_time,
                "credited": True,
            }
            self._store_credit(owner_id, event_seq, record)
            self._add_contribution(target_id, owner_id, effective, event_time)

        return {
            "duplicate": False,
            "credited": credited,
            "credited_to": owner_id,
            "effective": effective,
            "overheal": overheal,
        }

    def record_kill(self, event_seq, killer_id, victim_id, event_time):
        if self._settled:
            return {
                "ignored": "settled",
                "duplicate": False,
                "credited": False,
                "killer": None,
                "victim": victim_id,
                "assists": [],
            }
        if event_seq in self._seen_events or victim_id in self._dead_victims:
            return {
                "duplicate": True,
                "credited": False,
                "killer": None,
                "victim": victim_id,
                "assists": [],
            }
        self._seen_events.add(event_seq)

        owner_id = self._resolve_owner(killer_id)
        assists = self._resolve_assists(victim_id, owner_id, event_time)

        self._dead_victims.add(victim_id)
        self._last_assists[victim_id] = list(assists)
        self._kills[owner_id] = self._kills.get(owner_id, 0) + 1
        for assistant_id in assists:
            self._assist_counts[assistant_id] = self._assist_counts.get(assistant_id, 0) + 1

        record = {
            "event_seq": event_seq,
            "kind": "kill",
            "source_id": killer_id,
            "credited_to": owner_id,
            "target_id": victim_id,
            "amount": 0,
            "event_time": event_time,
            "credited": True,
            "killer": owner_id,
            "victim": victim_id,
            "assists": list(assists),
        }
        self._store_credit(owner_id, event_seq, record)

        return {
            "duplicate": False,
            "credited": True,
            "killer": owner_id,
            "victim": victim_id,
            "assists": list(assists),
        }

    def assist(self, victim_id):
        return list(self._last_assists.get(victim_id, ()))

    def credits_for_player(self, player_id):
        records = self._player_credits.get(player_id, ())
        ordered = sorted(records, key=lambda row: (row["event_time"], row["event_seq"]))
        return deepcopy(ordered)

    def credits_for_event(self, event_seq):
        return deepcopy(self._event_credits.get(event_seq, ()))

    def standings(self):
        return self._build_standings(self._victory_honor)

    def settle(self, winner_team=None):
        if self._settled:
            return deepcopy(self._settle_result)

        victory_honor = {}
        for player_id, player in self._players.items():
            if player["is_pet"] or player["kicked"]:
                continue
            if player["left_at"] is not None:
                continue
            if winner_team is not None and player["team"] != winner_team:
                continue
            victory_honor[player_id] = self.VICTORY_HONOR
        self._victory_honor = dict(victory_honor)

        standings = self._build_standings(victory_honor)
        mails = []
        for row in standings:
            if row["kicked"]:
                continue
            mails.append({
                "player_id": row["player_id"],
                "kills": row["kills"],
                "assists": row["assists"],
                "honor": row["honor"],
                "parts": 1,
            })

        result = {
            "frozen": True,
            "winner_team": winner_team,
            "standings": standings,
            "mails": mails,
        }
        self._settled = True
        self._settle_result = result
        return deepcopy(result)

    def _resolve_owner(self, source_id):
        player = self._players.get(source_id)
        if player is not None and player["is_pet"]:
            return player["pet_of"]
        return source_id

    def _is_present(self, player_id, event_time):
        player = self._players.get(player_id)
        if player is None or player["is_pet"]:
            return False
        left_at = player["left_at"]
        return left_at is None or event_time < left_at

    def _store_credit(self, player_id, event_seq, record):
        self._event_credits.setdefault(event_seq, []).append(record)
        self._player_credits.setdefault(player_id, []).append(record)

    def _add_contribution(self, victim_id, source_id, amount, event_time):
        self._contributions.setdefault(victim_id, []).append({
            "source": source_id,
            "amount": amount,
            "event_time": event_time,
        })

    def _resolve_assists(self, victim_id, killer_id, event_time):
        window_start = event_time - self.assist_window
        totals = {}
        for contribution in self._contributions.get(victim_id, ()):
            source_id = contribution["source"]
            if source_id == killer_id:
                continue
            if not self._is_present(source_id, event_time):
                continue
            contribution_time = contribution["event_time"]
            if contribution_time < window_start or contribution_time > event_time:
                continue
            aggregate = totals.setdefault(source_id, {"amount": 0, "latest": contribution_time})
            aggregate["amount"] += contribution["amount"]
            aggregate["latest"] = max(aggregate["latest"], contribution_time)

        ordered = sorted(
            totals.items(),
            key=lambda item: (-item[1]["amount"], item[1]["latest"], item[0]),
        )
        return [source_id for source_id, _ in ordered]

    def _build_standings(self, victory_honor):
        rows = []
        for player_id, player in self._players.items():
            if player["is_pet"]:
                continue
            kills = self._kills.get(player_id, 0)
            assists = self._assist_counts.get(player_id, 0)
            honor = (
                kills * self.HONOR_PER_KILL
                + assists * self.HONOR_PER_ASSIST
                + victory_honor.get(player_id, 0)
            )
            rows.append({
                "player_id": player_id,
                "team": player["team"],
                "kills": kills,
                "assists": assists,
                "honor": honor,
                "left": player["left_at"] is not None,
                "kicked": player["kicked"],
            })
        rows.sort(key=lambda row: (-row["honor"], row["player_id"]))
        return deepcopy(rows)
