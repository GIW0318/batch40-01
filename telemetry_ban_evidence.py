"""Telemetry based ban evidence.

Contract used by the tests
--------------------------
* ``__init__(threshold=3, appeal_days=30, retention_days=90, tz="UTC")``.
  ``retention_days`` must be >= ``appeal_days`` and ``tz`` must be "UTC",
  otherwise ValueError.
* ``ingest(account, match_id, events, segment=1, prev_hash=None,
  segment_hash=None, device_id=None, report_id=None, received_at=0)``.
  ``events`` is a list of dicts with keys ``seq`` (client order), ``t``
  (server epoch seconds) and ``kind`` in {"sample", "flag", "gap"}.
  Events are stored ordered by (t, seq).  A duplicate ``report_id`` (default
  "<account>:<match_id>") is ignored and returns False; otherwise True.
  Each report is linked into the account's hash chain: when ``prev_hash`` is
  given it must equal the previous report's ``segment_hash`` or the chain is
  marked broken at this segment.  ``segment_hash`` defaults to
  "<account>:<segment>".
* ``evaluate(account, now=0)`` recomputes from stored reports and never
  resets counters, so repeated calls (or reconnects) see the same totals.
  Returns keys account, status, flags, undecidable, per_day, chain_complete,
  whitelisted, fresh_flags, eligible.
  * "flag" events count 1 each, grouped by natural UTC day (``t // 86400``).
  * "gap" events count as undecidable, never as flags.
  * ``eligible`` is True when status is "active", the account is not
    whitelisted, the chain is complete, the maximum per-day flag count is at
    least ``threshold``, and (if a ban was invalidated by an appeal) at least
    one flag report was received strictly after that appeal.
* ``ban(account, reason="", now=0)`` requires eligible, else ValueError.  It
  sets status "banned" and appends a history record
  {"account", "reason", "at", "valid"}.
* ``appeal(account, now=0)`` requires status "banned", else ValueError; it
  sets status "active" and marks the latest ban record ``valid=False``.
* ``state(account)`` returns {"account", "status", "changed_at"}; status is
  "active" or "banned" and changed_at is an ISO-8601 UTC string ending in
  "+00:00" (epoch ``now`` converted with timezone.utc); it is None before
  the first change.
* ``history(account)`` returns the ban records in order.
* ``evidence(account=None)`` returns report records with keys account,
  match_id, report_id, segment, segment_hash, prev_hash, events, flags,
  undecidable, chain_ok, received_at.  events are the normalized dicts.
* ``chain_status(account)`` returns {"complete": bool, "broken_at": int|None}.
* ``set_whitelist(account, enabled=True)``; whitelisted accounts are never
  eligible.
* ``purge(now=0)`` deletes reports whose received_at is older than
  ``retention_days`` and returns the number removed.
* Device ids are recorded but never link accounts: a shared device does not
  make another account eligible.
"""

import copy
import threading
from collections import defaultdict
from datetime import datetime, timezone


class TelemetryBanEvidence:
    def __init__(self, threshold=3, appeal_days=30, retention_days=90,
                 tz="UTC"):
        if tz != "UTC":
            raise ValueError("tz must be UTC")
        if retention_days < appeal_days:
            raise ValueError("retention_days must be >= appeal_days")

        self.threshold = threshold
        self.appeal_days = appeal_days
        self.retention_days = retention_days
        self.tz = tz

        self._lock = threading.RLock()
        self._reports = defaultdict(list)
        self._report_ids = set()
        self._whitelist = set()
        self._states = {}
        self._history = defaultdict(list)
        self._last_appeal = {}
        self._devices = defaultdict(set)

    @staticmethod
    def _utc_iso(epoch):
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    def ingest(self, account, match_id, events, segment=1, prev_hash=None,
               segment_hash=None, device_id=None, report_id=None,
               received_at=0):
        with self._lock:
            if report_id is None:
                report_id = "{}:{}".format(account, match_id)
            if report_id in self._report_ids:
                return False

            normalized = [dict(event) for event in (events or [])]
            normalized.sort(key=lambda event: (
                event.get("t", 0), event.get("seq", 0)
            ))

            flags = sum(1 for event in normalized
                        if event.get("kind") == "flag")
            undecidable = sum(1 for event in normalized
                              if event.get("kind") == "gap")
            if segment_hash is None:
                segment_hash = "{}:{}".format(account, segment)

            report = {
                "account": account,
                "match_id": match_id,
                "report_id": report_id,
                "segment": segment,
                "segment_hash": segment_hash,
                "prev_hash": prev_hash,
                "events": normalized,
                "flags": flags,
                "undecidable": undecidable,
                "received_at": received_at,
                "device_id": device_id,
            }
            self._reports[account].append(report)
            self._report_ids.add(report_id)
            if device_id is not None:
                self._devices[device_id].add(account)
            return True

    def _chain_info(self, account):
        reports = self._reports.get(account, ())
        broken_at = None
        chain_ok = []
        previous = None

        for report in reports:
            ok = broken_at is None
            if previous is None:
                if report["prev_hash"] is not None or report["segment"] > 1:
                    ok = False
                    if broken_at is None:
                        broken_at = report["segment"]
            else:
                if report["prev_hash"] is not None:
                    if report["prev_hash"] != previous["segment_hash"]:
                        ok = False
                        if broken_at is None:
                            broken_at = report["segment"]
                if report["segment"] > previous["segment"] + 1:
                    ok = False
                    if broken_at is None:
                        broken_at = report["segment"]

            if broken_at is not None:
                ok = False
            chain_ok.append(ok)
            previous = report

        return {
            "complete": broken_at is None,
            "broken_at": broken_at,
        }, chain_ok

    def evaluate(self, account, now=0):
        with self._lock:
            reports = self._reports.get(account, ())
            flags = sum(report["flags"] for report in reports)
            undecidable = sum(report["undecidable"] for report in reports)

            per_day = defaultdict(int)
            for report in reports:
                for event in report["events"]:
                    if event.get("kind") == "flag":
                        per_day[event.get("t", 0) // 86400] += 1

            chain, _ = self._chain_info(account)
            status = self._states.get(account, {}).get("status", "active")
            whitelisted = account in self._whitelist

            appeal_at = self._last_appeal.get(account)
            if appeal_at is None:
                fresh_flags = flags
            else:
                fresh_flags = sum(
                    report["flags"] for report in reports
                    if report["received_at"] > appeal_at
                )

            max_per_day = max(per_day.values()) if per_day else 0
            eligible = (
                status == "active"
                and not whitelisted
                and chain["complete"]
                and max_per_day >= self.threshold
                and (appeal_at is None or fresh_flags > 0)
            )

            return {
                "account": account,
                "status": status,
                "flags": flags,
                "undecidable": undecidable,
                "per_day": dict(per_day),
                "chain_complete": chain["complete"],
                "whitelisted": whitelisted,
                "fresh_flags": fresh_flags,
                "eligible": eligible,
            }

    def ban(self, account, reason="", now=0):
        with self._lock:
            if not self.evaluate(account, now=now)["eligible"]:
                raise ValueError("account is not eligible for a ban")
            self._states[account] = {
                "status": "banned",
                "changed_at": now,
            }
            self._history[account].append({
                "account": account,
                "reason": reason,
                "at": now,
                "valid": True,
            })

    def appeal(self, account, now=0):
        with self._lock:
            state = self._states.get(account)
            if state is None or state["status"] != "banned":
                raise ValueError("account is not banned")
            self._states[account] = {
                "status": "active",
                "changed_at": now,
            }
            records = self._history[account]
            if records:
                records[-1]["valid"] = False
            self._last_appeal[account] = now

    def state(self, account):
        with self._lock:
            current = self._states.get(account)
            if current is None:
                return {
                    "account": account,
                    "status": "active",
                    "changed_at": None,
                }
            return {
                "account": account,
                "status": current["status"],
                "changed_at": self._utc_iso(current["changed_at"]),
            }

    def history(self, account):
        with self._lock:
            return copy.deepcopy(self._history.get(account, []))

    def evidence(self, account=None):
        with self._lock:
            if account is None:
                accounts = list(self._reports)
            else:
                accounts = [account]

            result = []
            for current in accounts:
                _, chain_ok = self._chain_info(current)
                for index, report in enumerate(self._reports.get(current, ())):
                    result.append({
                        "account": report["account"],
                        "match_id": report["match_id"],
                        "report_id": report["report_id"],
                        "segment": report["segment"],
                        "segment_hash": report["segment_hash"],
                        "prev_hash": report["prev_hash"],
                        "events": copy.deepcopy(report["events"]),
                        "flags": report["flags"],
                        "undecidable": report["undecidable"],
                        "chain_ok": chain_ok[index],
                        "received_at": report["received_at"],
                    })
            return result

    def chain_status(self, account):
        with self._lock:
            status, _ = self._chain_info(account)
            return status

    def set_whitelist(self, account, enabled=True):
        with self._lock:
            if enabled:
                self._whitelist.add(account)
            else:
                self._whitelist.discard(account)

    def purge(self, now=0):
        with self._lock:
            cutoff = now - self.retention_days * 86400
            removed = 0
            for account in list(self._reports):
                kept = [
                    report for report in self._reports[account]
                    if report["received_at"] >= cutoff
                ]
                removed += len(self._reports[account]) - len(kept)
                if kept:
                    self._reports[account] = kept
                else:
                    del self._reports[account]
            return removed
