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


class TelemetryBanEvidence:
    def __init__(self, threshold=3, appeal_days=30, retention_days=90,
                 tz="UTC"):
        self.threshold = threshold
        self.appeal_days = appeal_days
        self.retention_days = retention_days
        self.tz = tz

    def ingest(self, account, match_id, events, segment=1, prev_hash=None,
               segment_hash=None, device_id=None, report_id=None,
               received_at=0):
        raise NotImplementedError

    def evaluate(self, account, now=0):
        raise NotImplementedError

    def ban(self, account, reason="", now=0):
        raise NotImplementedError

    def appeal(self, account, now=0):
        raise NotImplementedError

    def state(self, account):
        raise NotImplementedError

    def history(self, account):
        raise NotImplementedError

    def evidence(self, account=None):
        raise NotImplementedError

    def chain_status(self, account):
        raise NotImplementedError

    def set_whitelist(self, account, enabled=True):
        raise NotImplementedError

    def purge(self, now=0):
        raise NotImplementedError
