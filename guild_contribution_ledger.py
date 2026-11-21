"""Guild contribution accrual, settlement, redemption and ledger."""

import math


def _num(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


class GuildContributionLedger:
    def __init__(self, period_seconds=7 * 86400.0, period_offset=12 * 3600.0,
                 wage_rate=1.0, compensation_cap=500):
        self.period_seconds = period_seconds
        self.period_offset = period_offset
        self.wage_rate = wage_rate
        self.compensation_cap = compensation_cap
        self.guilds = {}
        self.members = {}
        self.activities = {}
        self.ledger_entries = []
        self.requests = {}

    def _guild(self, guild_id):
        if guild_id not in self.guilds:
            raise KeyError("unknown guild: %r" % (guild_id,))
        return self.guilds[guild_id]

    def _member(self, guild_id, member):
        key = (guild_id, member)
        if key not in self.members:
            self.members[key] = {
                "guild_id": guild_id, "member": member, "role": "member",
                "joined_at": 0.0, "active": True, "balance": 0,
            }
        return self.members[key]

    def _period_for(self, guild, now):
        return math.floor((now - guild["period_offset"]) / self.period_seconds)

    def _open_period(self, guild, now):
        period = self._period_for(guild, now)
        while period in guild["settlements"]:
            period += 1
        return period

    def add_guild(self, guild_id, funds=0, period_offset=None, wage_rate=None,
                  compensation_cap=None):
        self.guilds[guild_id] = {
            "guild_id": guild_id,
            "funds": funds,
            "period_offset": self.period_offset if period_offset is None else period_offset,
            "wage_rate": self.wage_rate if wage_rate is None else wage_rate,
            "compensation_cap": (self.compensation_cap if compensation_cap is None
                                 else compensation_cap),
            "settlements": {},
        }
        return self.guilds[guild_id]

    def add_funds(self, guild_id, amount):
        guild = self._guild(guild_id)
        guild["funds"] += amount
        return guild["funds"]

    def add_member(self, guild_id, member, joined_at=0.0, active=True, role="member"):
        self._guild(guild_id)
        key = (guild_id, member)
        info = self.members.get(key, {"balance": 0})
        info.update({
            "guild_id": guild_id, "member": member, "role": role,
            "joined_at": joined_at, "active": active,
        })
        self.members[key] = info
        return info

    def accrue(self, guild_id, activity_id, member, amount, now=0.0, source="activity"):
        guild = self._guild(guild_id)
        key = (guild_id, activity_id, member)
        if key in self.activities:
            return {"duplicate": True, "applied": 0,
                    "activity_id": activity_id, "member": member}
        period = self._open_period(guild, now)
        info = self._member(guild_id, member)
        entry = {
            "kind": "accrue", "guild_id": guild_id, "period": period,
            "activity_id": activity_id, "member": member, "amount": amount,
            "source": source, "time": now, "payable": True, "paid": False,
        }
        self.ledger_entries.append(entry)
        self.activities[key] = entry
        info["balance"] += amount
        return {"duplicate": False, "applied": amount, "activity_id": activity_id,
                "member": member, "period": period, "balance": info["balance"]}

    def settle(self, guild_id, period=None, now=0.0):
        guild = self._guild(guild_id)
        if period is None:
            period = self._period_for(guild, now)
        if period in guild["settlements"]:
            result = dict(guild["settlements"][period])
            result["duplicate"] = True
            return result
        payable = []
        for entry in self.ledger_entries:
            if entry["guild_id"] != guild_id or entry["period"] != period:
                continue
            if entry["kind"] != "accrue" or entry["paid"] or not entry["payable"]:
                continue
            info = self.members.get((guild_id, entry["member"]))
            if info is None or not info["active"]:
                continue
            payable.append(entry)
        wages = []
        for entry in payable:
            amount = _num(entry["amount"] * guild["wage_rate"])
            wages.append({"member": entry["member"], "amount": amount,
                          "source": "wage"})
        total = sum(wage["amount"] for wage in wages)
        if total > guild["funds"]:
            raise ValueError("insufficient guild funds for settlement")
        guild["funds"] -= total
        for entry in payable:
            entry["paid"] = True
        for wage in wages:
            self.ledger_entries.append({
                "kind": "wage", "guild_id": guild_id, "period": period,
                "member": wage["member"], "amount": wage["amount"],
                "source": "wage", "time": now,
            })
        result = {"period": period, "total": total, "wages": wages,
                  "duplicate": False}
        guild["settlements"][period] = {
            "period": period, "total": total, "wages": [dict(w) for w in wages],
            "duplicate": False,
        }
        return result

    def redeem(self, guild_id, member, cost, item_id, request_id=None, now=0.0):
        guild = self._guild(guild_id)
        if request_id is not None:
            cached = self.requests.get((guild_id, request_id))
            if cached is not None:
                result = dict(cached)
                result["duplicate"] = True
                return result
        if cost <= 0:
            raise ValueError("redeem cost must be positive")
        info = self._member(guild_id, member)
        if info["balance"] < cost:
            raise ValueError("insufficient contribution balance")
        info["balance"] -= cost
        period = self._open_period(guild, now)
        self.ledger_entries.append({
            "kind": "redeem", "guild_id": guild_id, "period": period,
            "member": member, "item_id": item_id, "amount": cost,
            "request_id": request_id, "time": now,
        })
        result = {"redeemed": True, "duplicate": False, "cost": cost,
                  "balance": info["balance"], "item_id": item_id,
                  "request_id": request_id}
        if request_id is not None:
            self.requests[(guild_id, request_id)] = dict(result)
        return result

    def compensate(self, guild_id, actor, member, amount, reason,
                   request_id=None, now=0.0):
        guild = self._guild(guild_id)
        actor_info = self.members.get((guild_id, actor))
        if actor_info is None or actor_info.get("role") != "officer":
            raise ValueError("compensation requires an officer actor")
        if amount <= 0 or amount > guild["compensation_cap"]:
            raise ValueError("compensation amount outside allowed cap")
        if request_id is not None:
            cached = self.requests.get((guild_id, request_id))
            if cached is not None:
                result = dict(cached)
                result["duplicate"] = True
                return result
        info = self._member(guild_id, member)
        info["balance"] += amount
        period = self._open_period(guild, now)
        self.ledger_entries.append({
            "kind": "compensate", "guild_id": guild_id, "period": period,
            "member": member, "amount": amount, "actor": actor,
            "internal_note": reason, "request_id": request_id, "time": now,
        })
        result = {"compensated": True, "duplicate": False, "amount": amount,
                  "member": member, "period": period, "balance": info["balance"]}
        if request_id is not None:
            self.requests[(guild_id, request_id)] = dict(result)
        return result

    def leave(self, guild_id, member):
        self._guild(guild_id)
        info = self._member(guild_id, member)
        info["active"] = False
        for entry in self.ledger_entries:
            if (entry["guild_id"] == guild_id and entry.get("member") == member
                    and entry["kind"] == "accrue" and not entry["paid"]):
                entry["payable"] = False

    def join(self, guild_id, member, now=0.0):
        self._guild(guild_id)
        info = self._member(guild_id, member)
        info["active"] = True
        info["joined_at"] = now
        return info

    def balance(self, guild_id, member):
        info = self.members.get((guild_id, member))
        return info["balance"] if info is not None else 0

    def guild_funds(self, guild_id):
        return self._guild(guild_id)["funds"]

    def ledger(self, guild_id=None, period=None, public=False):
        snapshots = []
        for entry in self.ledger_entries:
            if guild_id is not None and entry["guild_id"] != guild_id:
                continue
            if period is not None and entry["period"] != period:
                continue
            snapshot = {k: v for k, v in entry.items()
                        if k not in ("paid", "payable")}
            if public:
                snapshot.pop("actor", None)
                snapshot.pop("internal_note", None)
            snapshots.append(snapshot)
        return snapshots

    def leaderboard(self, guild_id, period=None):
        stats = {}
        for entry in self.ledger_entries:
            if entry["guild_id"] != guild_id:
                continue
            if entry["kind"] not in ("accrue", "compensate"):
                continue
            if period is not None and entry["period"] != period:
                continue
            stat = stats.setdefault(entry["member"],
                                    {"contribution": 0, "first": entry["time"]})
            stat["contribution"] += entry["amount"]
            stat["first"] = min(stat["first"], entry["time"])
        ranking = [{"member": member, "contribution": stat["contribution"]}
                   for member, stat in stats.items()]
        ranking.sort(key=lambda row: (-row["contribution"],
                                      stats[row["member"]]["first"]))
        return ranking
