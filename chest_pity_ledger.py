"""Chest pity ledger.

Contract used by the tests
--------------------------
* ``configure_pool(pool, quality, hard_pity, pool_version="v1",
  prob_version="base", inherit_across_seasons=False)`` registers a pool.
* An account must call ``login`` before ``open``, ``pity`` or ``reset``;
  otherwise RuntimeError.  ``login`` returns a snapshot dict keyed
  ``"<pool>:<quality>"`` and never resets an existing account.
* Counters are kept per (account, pool, quality); accounts and qualities
  never share a counter.
* ``open(account, pool, quality, count=1, request_id=None, now=0, season=1)``
  accepts 1..10 (10 is one ten-pull batch).  The counter grows by count once;
  pity is evaluated once for the batch.  When
  ``pity_before + count >= hard_pity`` the batch triggers exactly once:
  ``pity_after`` is 0 and the last item has rarity "rare"; otherwise every
  item is "common".
* Items are ``{"index": i, "item_id": "<request_id>#<i>", "rarity": ...}``
  with i from 1..count.  A generated request_id is returned in the result.
* ``request_id`` is idempotent: the same dict is returned again and neither
  counters nor history change.
* ``set_rate_up(pool, start, end, prob_version="rateup")``; the window is
  half-open [start, end) on the ``now`` argument.  The used probability
  version is recorded in the result and history.
* Result keys: request_id, account, pool, quality, count, items,
  pity_before, pity_after, pool_version, prob_version, season, at,
  rolled_back.
* ``rollback(request_id)`` restores pity_before and marks the record
  rolled_back True; unknown or already rolled back ids raise ValueError.
* ``rollover(account, pool, quality, new_season)`` keeps the counter when
  ``inherit_across_seasons`` is True and resets it to 0 otherwise; returns
  the resulting counter.
* ``history(account=None)`` returns records in insertion order.
* Unknown pool/quality and a count outside 1..10 raise ValueError.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class _PoolConfig:
    hard_pity: int
    pool_version: str
    prob_version: str
    inherit_across_seasons: bool


@dataclass
class _AccountState:
    pity: Dict[str, int]


class ChestPityLedger:
    def __init__(self):
        self._configs: Dict[Tuple[str, str], _PoolConfig] = {}
        self._rate_ups: Dict[str, List[Tuple[int, int, str]]] = {}
        self._accounts: Dict[str, _AccountState] = {}
        self._logged_in: set[str] = set()
        self._requests: Dict[str, dict] = {}
        self._history: List[dict] = []
        self._lock = threading.RLock()

    @staticmethod
    def _key(pool: str, quality: str) -> str:
        return f"{pool}:{quality}"

    def _config(self, pool: str, quality: str) -> _PoolConfig:
        try:
            return self._configs[(pool, quality)]
        except KeyError:
            raise ValueError(f"unknown pool/quality: {pool}/{quality}") from None

    def _require_login(self, account: str) -> _AccountState:
        if account not in self._logged_in:
            raise RuntimeError(f"account {account!r} has not logged in")
        state = self._accounts.get(account)
        if state is None:
            state = _AccountState(pity={})
            self._accounts[account] = state
        return state

    def _prob_version(self, pool: str, now: int, config: _PoolConfig) -> str:
        for start, end, version in reversed(self._rate_ups.get(pool, [])):
            if start <= now < end:
                return version
        return config.prob_version

    def configure_pool(self, pool, quality, hard_pity, pool_version="v1",
                       prob_version="base", inherit_across_seasons=False):
        with self._lock:
            self._configs[(pool, quality)] = _PoolConfig(
                hard_pity=hard_pity,
                pool_version=pool_version,
                prob_version=prob_version,
                inherit_across_seasons=inherit_across_seasons,
            )

    def set_rate_up(self, pool, start, end, prob_version="rateup"):
        with self._lock:
            if not any(configured_pool == pool
                       for configured_pool, _ in self._configs):
                raise ValueError(f"unknown pool: {pool}")
            self._rate_ups.setdefault(pool, []).append(
                (start, end, prob_version)
            )

    def login(self, account):
        with self._lock:
            state = self._accounts.get(account)
            if state is None:
                state = _AccountState(pity={})
                self._accounts[account] = state
            for pool, quality in self._configs:
                state.pity.setdefault(self._key(pool, quality), 0)
            self._logged_in.add(account)
            return dict(state.pity)

    def open(self, account, pool, quality, count=1, request_id=None, now=0,
             season=1):
        with self._lock:
            state = self._require_login(account)

            if request_id is not None and request_id in self._requests:
                return self._requests[request_id]

            if isinstance(count, bool) or not isinstance(count, int):
                raise ValueError("count must be an integer between 1 and 10")
            if not 1 <= count <= 10:
                raise ValueError("count must be between 1 and 10")

            config = self._config(pool, quality)
            if request_id is None:
                request_id = f"req-{uuid.uuid4().hex}"
                while request_id in self._requests:
                    request_id = f"req-{uuid.uuid4().hex}"

            key = self._key(pool, quality)
            pity_before = state.pity.get(key, 0)
            triggered = pity_before + count >= config.hard_pity
            pity_after = 0 if triggered else pity_before + count
            prob_version = self._prob_version(pool, now, config)

            items = [
                {
                    "index": index,
                    "item_id": f"{request_id}#{index}",
                    "rarity": (
                        "rare"
                        if triggered and index == count
                        else "common"
                    ),
                }
                for index in range(1, count + 1)
            ]

            record = {
                "request_id": request_id,
                "account": account,
                "pool": pool,
                "quality": quality,
                "count": count,
                "items": items,
                "pity_before": pity_before,
                "pity_after": pity_after,
                "pool_version": config.pool_version,
                "prob_version": prob_version,
                "season": season,
                "at": now,
                "rolled_back": False,
            }

            # Counter mutation and the operation record are committed together.
            state.pity[key] = pity_after
            self._requests[request_id] = record
            self._history.append(record)
            return record

    def pity(self, account, pool, quality):
        with self._lock:
            state = self._require_login(account)
            self._config(pool, quality)
            return state.pity.get(self._key(pool, quality), 0)

    def reset(self, account, pool, quality):
        with self._lock:
            state = self._require_login(account)
            self._config(pool, quality)
            state.pity[self._key(pool, quality)] = 0
            return 0

    def rollback(self, request_id):
        with self._lock:
            record = self._requests.get(request_id)
            if record is None or record["rolled_back"]:
                raise ValueError(f"unknown or already rolled back request: {request_id}")

            state = self._accounts.get(record["account"])
            if state is not None:
                key = self._key(record["pool"], record["quality"])
                state.pity[key] = record["pity_before"]
            record["rolled_back"] = True
            return record

    def rollover(self, account, pool, quality, new_season):
        with self._lock:
            config = self._config(pool, quality)
            state = self._accounts.get(account)
            if state is None:
                state = _AccountState(pity={})
                self._accounts[account] = state

            key = self._key(pool, quality)
            current = state.pity.get(key, 0)
            result = current if config.inherit_across_seasons else 0
            state.pity[key] = result
            return result

    def history(self, account=None):
        with self._lock:
            if account is None:
                return list(self._history)
            return [
                record for record in self._history
                if record["account"] == account
            ]
