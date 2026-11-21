"""Season rollover roster, reset, reward grant and claims."""

from __future__ import annotations

import copy
import threading


class SeasonRolloverLedger:
    """Coordinates the one-time work performed when a season rolls over.

    The ledger deliberately keeps the rollover roster as an immutable snapshot.
    Every destructive or rewarding operation is based on that snapshot, so
    players added (or ranks changed) after the snapshot cannot affect the
    settlement.  Operations that may be retried are idempotent per season.
    """

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

        self._lock = threading.RLock()
        self._reset_states = {}
        self._tokens_reclaimed = {}
        self.archived_ranks = {}

    # ------------------------------------------------------------------
    # Player storage
    # ------------------------------------------------------------------
    @staticmethod
    def _normalise_player(data):
        player = dict(data)
        rank = player.get("rank", "bronze")
        player["rank"] = rank
        player["peak"] = player.get("peak") or rank
        player["placement_done"] = bool(player.get("placement_done", True))
        player["banned"] = bool(player.get("banned", False))
        player["streak"] = player.get("streak", 0)
        player["tokens"] = player.get("tokens", 0)
        return player

    def load_players(self, players):
        """Replace the player store with ``players`` (an account -> data map)."""
        with self._lock:
            self.players = {
                account: self._normalise_player(data)
                for account, data in dict(players or {}).items()
            }
            return self

    def add_player(self, account, rank="bronze", peak=None, placement_done=True,
                   banned=False, streak=0, tokens=0):
        with self._lock:
            player = {
                "rank": rank,
                "peak": peak or rank,
                "placement_done": placement_done,
                "banned": banned,
                "streak": streak,
                "tokens": tokens,
            }
            self.players[account] = self._normalise_player(player)
            return self.players[account]

    def update_player(self, account, **fields):
        with self._lock:
            if account not in self.players:
                self.players[account] = self._normalise_player({})
            player = self.players[account]
            player.update(fields)
            self.players[account] = self._normalise_player(player)
            return self.players[account]

    def get_player(self, account):
        return self.players[account]

    def player_accounts(self):
        return list(self.players)

    def pending_accounts(self):
        return sorted(self.pending_pool)

    # ------------------------------------------------------------------
    # Roster snapshot
    # ------------------------------------------------------------------
    def build_roster(self, now=0.0):
        """Freeze the players and their reward-relevant fields for the season."""
        with self._lock:
            self.roster_version += 1
            entries = []
            for account, player in self.players.items():
                peak = player.get("peak") or player.get("rank", "bronze")
                current = player.get("rank", "bronze")
                entries.append({
                    "account": account,
                    "rank": current,
                    "current_tier": current,
                    "peak": peak,
                    "peak_tier": peak,
                    "placement_done": bool(player.get("placement_done", True)),
                    "banned": bool(player.get("banned", False)),
                    "streak": player.get("streak", 0),
                    "tokens": player.get("tokens", 0),
                })

            self.roster = {
                "season_id": self.season_id,
                "created_at": now,
                "version": self.roster_version,
                "entries": entries,
            }
            return copy.deepcopy(self.roster)

    def _roster_entries(self):
        if self.roster is None:
            return []
        return list(self.roster.get("entries", []))

    def _live_banned(self, entry):
        account = entry["account"]
        player = self.players.get(account)
        if player is None:
            return bool(entry.get("banned", False))
        return bool(player.get("banned", entry.get("banned", False)))

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------
    def _reset_state(self, season_id):
        return self._reset_states.setdefault(season_id, {
            "checkpoint": 0,
            "done": set(),
            "pending": set(),
            "complete": False,
            "failed": False,
            "archived": {},
        })

    def _reset_one(self, entry, season_id, state):
        account = entry["account"]
        player = self.players.get(account)
        if player is None:
            state["done"].add(account)
            return

        old_rank = player.get("rank", entry.get("rank", "bronze"))
        old_peak = player.get("peak", entry.get("peak", old_rank))
        state["archived"][account] = {
            "season_id": season_id,
            "rank": old_rank,
            "peak": old_peak,
        }
        self.archived_ranks[account] = state["archived"][account]

        if not player.get("placement_done", entry.get("placement_done", True)):
            # Preserve the provisional rank and let the placement pool handle
            # the account.  The old season's streak must not leak forward.
            state["pending"].add(account)
        else:
            player["rank"] = self.reset_rules.get(old_rank, old_rank)
            # ``peak`` is historical and intentionally left untouched.

        player["streak"] = 0
        state["done"].add(account)

    def reset(self, season_id=None, fail_after=None):
        """Apply the soft reset to the frozen roster in resumable batches.

        ``fail_after`` is a test/fault-injection hook expressed as a number of
        batches.  A failed run commits everything before the failure; calling
        ``reset`` again resumes at the recorded checkpoint.
        """
        season = season_id or self.season_id
        with self._lock:
            state = self._reset_state(season)
            entries = self._roster_entries()

            if state["complete"]:
                self._sync_reset_globals(state)
                return self._reset_result(season, state, entries, newly=[])

            batch_size = max(1, int(self.batch_size))
            start = min(state["checkpoint"], len(entries))
            batches = 0
            newly = []
            state["failed"] = False

            while start < len(entries):
                if fail_after is not None and batches >= fail_after:
                    state["failed"] = True
                    break

                batch = entries[start:start + batch_size]
                for entry in batch:
                    if entry["account"] in state["done"]:
                        continue
                    self._reset_one(entry, season, state)
                    newly.append(entry["account"])

                start += len(batch)
                state["checkpoint"] = start
                batches += 1

            state["complete"] = start >= len(entries)
            if state["complete"]:
                state["failed"] = False

            self._sync_reset_globals(state)
            return self._reset_result(season, state, entries, newly=newly)

    def _sync_reset_globals(self, state):
        self.reset_done = set(state["done"])
        self.pending_pool = set(state["pending"])
        self.checkpoint = state["checkpoint"]

    def _reset_result(self, season, state, entries, newly):
        processed = len(state["done"])
        return {
            "season_id": season,
            "complete": state["complete"],
            "failed": state["failed"],
            "processed": processed,
            "newly_processed": len(newly),
            "total": len(entries),
            "checkpoint": state["checkpoint"],
            "remaining": max(0, len(entries) - state["checkpoint"]),
            "pending_pool": sorted(state["pending"]),
            "accounts": list(newly),
        }

    # ------------------------------------------------------------------
    # Rewards and claims
    # ------------------------------------------------------------------
    def grant(self, season_id=None, now=0.0):
        """Grant rewards from the frozen roster, once per season."""
        season = season_id or self.season_id
        with self._lock:
            if season in self.grant_results:
                result = copy.deepcopy(self.grant_results[season])
                result["duplicate"] = True
                return result

            mails = []
            filtered_banned = []
            for entry in self._roster_entries():
                account = entry["account"]
                if self._live_banned(entry):
                    filtered_banned.append(account)
                    continue

                # Settlement is based on the snapshot, not on a player's
                # (possibly already reset) live rank.
                current_tier = entry.get("current_tier", entry.get("rank"))
                peak_tier = entry.get("peak_tier", entry.get("peak") or current_tier)
                amount = self.rewards.get(peak_tier, 0)
                mails.append({
                    "season_id": season,
                    "account": account,
                    "tier": peak_tier,
                    "peak_tier": peak_tier,
                    "current_tier": current_tier,
                    "amount": amount,
                    "granted_at": now,
                    "expires_at": now + self.mail_ttl,
                    "claimed": False,
                })

            result = {
                "season_id": season,
                "granted_at": now,
                "mails": mails,
                "count": len(mails),
                "filtered_banned": filtered_banned,
                "duplicate": False,
            }
            self.grant_results[season] = copy.deepcopy(result)
            return result

    def claim(self, account, tier, season_id=None, now=0.0):
        """Claim a granted reward; uniqueness is per season/account/tier."""
        season = season_id or self.season_id
        key = (season, account, tier)

        with self._lock:
            granted = self.grant_results.get(season)
            mail = None
            if granted is not None:
                for candidate in granted.get("mails", []):
                    if (candidate.get("account") == account
                            and candidate.get("peak_tier") == tier):
                        mail = candidate
                        break

            if mail is None:
                return {
                    "claimed": False,
                    "reason": "no_reward",
                    "season_id": season,
                    "account": account,
                    "tier": tier,
                }

            if key in self.claims:
                return {
                    "claimed": False,
                    "reason": "already_claimed",
                    "season_id": season,
                    "account": account,
                    "tier": tier,
                }

            if now >= mail.get("expires_at", 0):
                return {
                    "claimed": False,
                    "reason": "expired",
                    "season_id": season,
                    "account": account,
                    "tier": tier,
                }

            # Record the claim before returning the amount.  This keeps
            # duplicate/concurrent clicks from granting the reward twice.
            self.claims.add(key)
            mail["claimed"] = True
            mail["claimed_at"] = now
            stored = self.grant_results.get(season)
            if stored is not None:
                for candidate in stored.get("mails", []):
                    if (candidate.get("account") == account
                            and candidate.get("peak_tier") == tier):
                        candidate["claimed"] = True
                        candidate["claimed_at"] = now
                        break

            return {
                "claimed": True,
                "reason": None,
                "season_id": season,
                "account": account,
                "tier": tier,
                "amount": mail.get("amount", 0),
                "claimed_at": now,
            }

    def reclaim_tokens(self, season_id=None):
        """Reclaim old-season tokens once, after that season was granted."""
        season = season_id or self.season_id
        with self._lock:
            if season not in self.grant_results:
                return {
                    "season_id": season,
                    "reclaimed": 0,
                    "accounts": [],
                    "reason": "grant_not_completed",
                }

            if season in self._tokens_reclaimed:
                return {
                    "season_id": season,
                    "reclaimed": 0,
                    "accounts": [],
                    "reason": "already_reclaimed",
                }

            total = 0
            accounts = []
            for entry in self._roster_entries():
                account = entry["account"]
                player = self.players.get(account)
                if player is None:
                    continue
                tokens = player.get("tokens", 0)
                if tokens:
                    total += tokens
                    player["tokens"] = 0
                    accounts.append(account)

            self._tokens_reclaimed[season] = total
            return {
                "season_id": season,
                "reclaimed": total,
                "accounts": accounts,
                "reason": None,
            }
