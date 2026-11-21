"""Admission and revocation cache for access tokens."""

from collections import OrderedDict


class TokenRevocationCache:
    """Caches token admission decisions and tracks revocations per issuer.

    All time comparisons use the ``now`` supplied by the caller.  Revoked
    tokens are indexed by ``(issuer, token_id)`` so that equal token ids from
    different issuers cannot interfere with one another.
    """

    def __init__(self, negative_limit=1000, positive_limit=1000):
        self.negative_limit = max(0, int(negative_limit))
        self.positive_limit = max(0, int(positive_limit))

        # A negative entry records a token that was not revoked when last
        # checked.  Scanner traffic is what makes this cache grow, so it is the
        # one that must be bounded.
        self._negative = OrderedDict()

        # Positive entries mirror revocation decisions.  The authoritative
        # revocation table is kept separately so that evicting a cached lookup
        # can never make a revoked token admissible again.
        self._positive = OrderedDict()
        self._revoked_tokens = {}
        self._token_seq = {}

        self._account_revocations = {}
        self._account_seq = {}

        self._last_seq = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @staticmethod
    def _token_key(issuer, token_id):
        return issuer, token_id

    @staticmethod
    def _account_key(issuer, subject):
        return issuer, subject

    def _bound_positive(self):
        while len(self._positive) > self.positive_limit:
            self._positive.popitem(last=False)
            self._evictions += 1

    def _remember_negative(self, key, record):
        if self.negative_limit <= 0:
            return
        self._negative[key] = record
        self._negative.move_to_end(key)
        while len(self._negative) > self.negative_limit:
            self._negative.popitem(last=False)
            self._evictions += 1

    def _remember_positive(self, key, record):
        if self.positive_limit <= 0:
            return
        self._positive[key] = record
        self._positive.move_to_end(key)
        self._bound_positive()

    def _account_revoked(self, issuer, subject, issued_at):
        entry = self._account_revocations.get(
            self._account_key(issuer, subject)
        )
        return entry is not None and issued_at < entry["cutoff"]

    def check(self, issuer, token_id, subject, issued_at, expires_at, now):
        key = self._token_key(issuer, token_id)

        if key in self._negative or key in self._positive:
            self._hits += 1
        else:
            self._misses += 1

        if key in self._revoked_tokens:
            return {"allowed": False, "reason": "revoked_token"}

        if self._account_revoked(issuer, subject, issued_at):
            return {"allowed": False, "reason": "revoked_account"}

        if expires_at <= now:
            return {"allowed": False, "reason": "expired"}

        # The token is currently admissible.  Any old revocation lookup for
        # this exact issuer/token pair is now stale.
        self._positive.pop(key, None)
        self._remember_negative(
            key,
            {
                "issuer": issuer,
                "token_id": token_id,
                "subject": subject,
                "issued_at": issued_at,
                "expires_at": expires_at,
            },
        )
        return {"allowed": True, "reason": None}

    def revoke_token(self, issuer, token_id, now, reason="logout"):
        key = self._token_key(issuer, token_id)
        entry = self._revoked_tokens.get(key)
        if entry is None:
            entry = {
                "issuer": issuer,
                "token_id": token_id,
                "revoked_at": now,
                "reason": reason,
            }
            self._revoked_tokens[key] = entry

        # A previously cached "not revoked" result must not mask this event.
        self._negative.pop(key, None)
        self._remember_positive(key, dict(entry))
        return True

    def revoke_account(self, issuer, subject, cutoff, reason="account_banned"):
        key = self._account_key(issuer, subject)
        current = self._account_revocations.get(key)
        if current is None:
            self._account_revocations[key] = {
                "issuer": issuer,
                "subject": subject,
                "cutoff": cutoff,
                "reason": reason,
            }
        else:
            current["cutoff"] = max(current["cutoff"], cutoff)

        revoked = 0
        for token_key, record in list(self._negative.items()):
            if (
                record["issuer"] == issuer
                and record["subject"] == subject
                and record["issued_at"] < cutoff
            ):
                del self._negative[token_key]
                revoked += 1
        return revoked

    def apply_revocations(self, events, now):
        applied = 0
        ignored = 0

        for event in events or ():
            issuer = event.get("issuer")
            seq = event.get("seq")
            is_revoked = event.get("revoked", True)

            if event.get("token_id") is not None:
                key = self._token_key(issuer, event["token_id"])
                current_seq = self._token_seq.get(key)
                if seq is not None and current_seq is not None and seq <= current_seq:
                    ignored += 1
                    continue

                if seq is None and is_revoked == (key in self._revoked_tokens):
                    ignored += 1
                    continue

                if seq is not None:
                    self._token_seq[key] = seq
                    self._last_seq = max(self._last_seq, seq)

                if is_revoked:
                    entry = {
                        "issuer": issuer,
                        "token_id": event["token_id"],
                        "revoked_at": event.get("revoked_at", now),
                        "reason": event.get("reason", "logout"),
                    }
                    self._revoked_tokens[key] = entry
                    self._negative.pop(key, None)
                    self._remember_positive(key, dict(entry))
                else:
                    self._revoked_tokens.pop(key, None)
                    self._positive.pop(key, None)
                applied += 1
                continue

            if event.get("subject") is not None:
                key = self._account_key(issuer, event["subject"])
                current_seq = self._account_seq.get(key)
                if seq is not None and current_seq is not None and seq <= current_seq:
                    ignored += 1
                    continue

                if seq is not None:
                    self._account_seq[key] = seq
                    self._last_seq = max(self._last_seq, seq)

                if is_revoked:
                    cutoff = event.get("cutoff", event.get("revoked_at", now))
                    current = self._account_revocations.get(key)
                    if current is None:
                        self._account_revocations[key] = {
                            "issuer": issuer,
                            "subject": event["subject"],
                            "cutoff": cutoff,
                            "reason": event.get("reason", "account_banned"),
                        }
                    else:
                        current["cutoff"] = max(current["cutoff"], cutoff)

                    for token_key, record in list(self._negative.items()):
                        if (
                            record["issuer"] == issuer
                            and record["subject"] == event["subject"]
                            and record["issued_at"] < cutoff
                        ):
                            del self._negative[token_key]
                else:
                    self._account_revocations.pop(key, None)
                applied += 1
                continue

            ignored += 1

        return {
            "applied": applied,
            "ignored": ignored,
            "last_seq": self._last_seq,
        }

    def snapshot(self):
        """Return a detached view of cache state and counters."""
        return {
            "revoked_tokens": [
                dict(entry) for entry in self._revoked_tokens.values()
            ],
            "account_revocations": [
                dict(entry) for entry in self._account_revocations.values()
            ],
            "negative_entries": len(self._negative),
            "positive_entries": len(self._positive),
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "last_seq": self._last_seq,
        }
