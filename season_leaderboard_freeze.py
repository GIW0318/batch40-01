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

    @staticmethod
    def _tie_key(submitted_at, account):
        return (float(submitted_at), str(account))

    def _upsert(self, account, score, submitted_at):
        key = self._tie_key(submitted_at, account)
        current = self.entries.get(account)
        if current is None or score > current["score"]:
            self.entries[account] = {
                "account": account,
                "score": score,
                "tie_key": key,
            }
        elif score == current["score"] and key < current["tie_key"]:
            current["tie_key"] = key

    @staticmethod
    def _rank_entries(entries):
        ordered = sorted(entries, key=lambda item: (-item["score"], item["tie_key"]))
        ranked = []
        previous_score = None
        previous_rank = 0
        for index, entry in enumerate(ordered):
            if previous_score is not None and entry["score"] == previous_score:
                rank = previous_rank
            else:
                rank = index + 1
                previous_score = entry["score"]
                previous_rank = rank
            ranked.append({
                "account": entry["account"],
                "score": entry["score"],
                "tie_key": entry["tie_key"],
                "rank": rank,
            })
        return ranked

    def _snapshot(self):
        self.version += 1
        self.snapshots[self.version] = self._rank_entries(self.entries.values())
        return self.version

    def _frozen_list(self, version=None):
        version = self.version if version is None else version
        return self.snapshots.get(version, [])[:self.top_n]

    def submit(self, account, score, match_id=None, ended_at=0.0,
               submitted_at=0.0, server_id=None):
        if ended_at > self.season_end:
            return {"accepted": False, "reason": "next_season",
                    "season_id": self.season_id}
        if self.frozen_at is not None:
            return {"accepted": False, "reason": "frozen",
                    "season_id": self.season_id}
        self._upsert(account, score, submitted_at)
        return {"accepted": True, "season_id": self.season_id}

    def merge_server(self, server_id, entries):
        if self.frozen_at is not None:
            raise ValueError("season %s is frozen" % self.season_id)
        for item in entries:
            self._upsert(item["account"], item["score"],
                         item.get("submitted_at", 0.0))
        return {"merged": len(entries), "server_id": server_id}

    def freeze(self, now=None):
        if self.frozen_at is None:
            self.frozen_at = now
            self._snapshot()
        return {
            "version": self.version,
            "frozen_at": self.frozen_at,
            "entries": [dict(entry) for entry in self._frozen_list()],
        }

    def rank(self, page=1, page_size=20, version=None):
        if self.frozen_at is not None:
            source = self._frozen_list(version)
        else:
            source = self._rank_entries(self.entries.values())
        total = len(source)
        start = max(page - 1, 0) * page_size
        items = [dict(entry) for entry in source[start:start + page_size]]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "version": self.version if self.frozen_at is not None else None,
        }

    def _tier_for(self, rank):
        for tier in self.tiers:
            if tier["min_rank"] <= rank <= tier["max_rank"]:
                return tier
        return None

    def _compute_awards(self, version):
        awards = []
        for entry in self._frozen_list(version):
            tier = self._tier_for(entry["rank"])
            if tier is None:
                continue
            awards.append({
                "account": entry["account"],
                "rank": entry["rank"],
                "tier": tier["tier"],
                "amount": tier["reward"],
            })
        return awards

    def award(self, version=None):
        if self.frozen_at is None:
            raise ValueError("season %s is not frozen" % self.season_id)
        if version is not None and version != self.version:
            if version not in self.snapshots:
                raise ValueError("unknown frozen version %r" % (version,))
            if version not in self.award_runs:
                self.award_runs[version] = {
                    "version": version,
                    "awards": self._compute_awards(version),
                    "supplement": [],
                }
            run = self.award_runs[version]
            return {
                "version": run["version"],
                "awards": [dict(item) for item in run["awards"]],
                "supplement": [dict(item) for item in run["supplement"]],
            }
        version = self.version
        awards = self._compute_awards(version)
        supplement = []
        for item in awards:
            already = self.awarded.get(item["account"], 0)
            delta = item["amount"] - already
            if delta > 0:
                supplement.append(dict(item, amount=delta))
            self.awarded[item["account"]] = max(already, item["amount"])
        self.award_runs[version] = {
            "version": version,
            "awards": awards,
            "supplement": supplement,
        }
        return {
            "version": version,
            "awards": [dict(item) for item in awards],
            "supplement": [dict(item) for item in supplement],
        }

    def awarded_amount(self, account):
        return self.awarded.get(account, 0)

    def appeal(self, account, new_score, case_id=None, now=0.0):
        current = self.entries.get(account)
        before = current["score"] if current is not None else None
        self.appeals.append({
            "account": account,
            "case_id": case_id,
            "before": before,
            "after": new_score,
            "at": now,
        })
        tie_key = current["tie_key"] if current is not None else self._tie_key(now, account)
        self.entries[account] = {
            "account": account,
            "score": new_score,
            "tie_key": tie_key,
        }
        if self.frozen_at is not None:
            self._snapshot()
        return {"appealed": True, "account": account,
                "before": before, "after": new_score}

    def remove(self, account, reason="violation", now=0.0):
        existed = account in self.entries
        self.entries.pop(account, None)
        self.removals.append({"account": account, "reason": reason, "at": now})
        if self.frozen_at is not None:
            self._snapshot()
        return {"removed": existed, "account": account, "reason": reason}
