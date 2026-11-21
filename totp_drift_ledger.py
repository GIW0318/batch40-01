"""Multi-tenant TOTP drift compensation and consumption ledger."""

import base64
import hashlib
import hmac
import math
import struct
import uuid

STEP_SECONDS = 30


def _normalize_base32(secret):
    """Normalize Base32 input: strip spaces, uppercase, re-pad with '='."""
    if isinstance(secret, bytes):
        secret = secret.decode("ascii")
    text = "".join(secret.split()).upper().rstrip("=")
    text += "=" * ((8 - len(text) % 8) % 8)
    return base64.b32decode(text)


def _hotp(key, step, digits):
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def _utc_step(now):
    """Time step is always derived from UTC epoch seconds via floor."""
    return int(math.floor(now / STEP_SECONDS))


class TotpDriftLedger:
    """Verifies TOTP codes with per-tenant windows and shared failure counts.

    State lives in the externally supplied ``store`` dict so that every
    gateway process sharing the same store sees the same failure counters,
    consumed codes and audit trail.
    """

    def __init__(self, tenants, store=None):
        self.tenants = tenants
        self.store = {} if store is None else store
        # Authenticators keyed by (tenant, user) -> {auth_id: record}.
        self.store.setdefault("authenticators", {})
        # Consecutive failure counters keyed by (tenant, user).
        self.store.setdefault("failures", {})
        # Consumed (tenant, user) -> set of (auth_id, key_id, step).
        self.store.setdefault("consumed", {})
        # Per (tenant, user) accepted drift compensation, in steps.
        self.store.setdefault("drift", {})
        self.store.setdefault("audit", [])

    def _user_auths(self, tenant, user):
        return self.store["authenticators"].setdefault((tenant, user), {})

    def enroll(self, tenant, user, secret, digits=6, authenticator_id=None):
        if tenant not in self.tenants:
            raise KeyError(tenant)
        if authenticator_id is None:
            authenticator_id = uuid.uuid4().hex
        auths = self._user_auths(tenant, user)
        if authenticator_id in auths:
            raise ValueError("authenticator already enrolled: %s" % authenticator_id)
        auths[authenticator_id] = {
            "id": authenticator_id,
            "digits": digits,
            "keys": [{
                "key_id": uuid.uuid4().hex,
                "secret": _normalize_base32(secret),
                "overlap_until": None,
            }],
        }
        return authenticator_id

    def rotate(self, tenant, user, authenticator_id, new_secret, overlap_until):
        auths = self._user_auths(tenant, user)
        record = auths[authenticator_id]
        # The old key stays verifiable during the explicit overlap window;
        # the new key is verifiable immediately and remains so afterwards.
        for key in record["keys"]:
            if key["overlap_until"] is None:
                key["overlap_until"] = overlap_until
        record["keys"].append({
            "key_id": uuid.uuid4().hex,
            "secret": _normalize_base32(new_secret),
            "overlap_until": None,
        })
        return record["keys"][-1]["key_id"]

    def unbind(self, tenant, user, authenticator_id):
        auths = self._user_auths(tenant, user)
        if authenticator_id not in auths:
            return False
        del auths[authenticator_id]
        self.store["consumed"].pop((tenant, user), None)
        return True

    def verify(self, tenant, user, code, now):
        current_step = _utc_step(now)

        def fail(reason, drift):
            failures = self.store["failures"]
            count = failures.get((tenant, user), 0) + 1
            failures[(tenant, user)] = count
            entry = {
                "tenant": tenant,
                "user": user,
                "ok": False,
                "reason": reason,
                "step": current_step,
                "drift": drift,
                "failures": count,
            }
            self._record_audit(entry)
            return entry

        config = self.tenants.get(tenant)
        if config is None:
            return fail("unknown_tenant", 0)

        failures = self.store["failures"]
        count = failures.get((tenant, user), 0)
        if count >= config["max_failures"]:
            entry = {
                "tenant": tenant,
                "user": user,
                "ok": False,
                "reason": "locked",
                "step": current_step,
                "drift": self.store["drift"].get((tenant, user), 0),
                "failures": count,
            }
            self._record_audit(entry)
            return entry

        text = "" if code is None else "".join(str(code).split())
        if not text or not text.isdigit():
            return fail("invalid_code", 0)

        window = config["window"]
        auths = self._user_auths(tenant, user)
        consumed = self.store["consumed"].setdefault((tenant, user), set())

        candidates = []
        for record in auths.values():
            if record["digits"] != len(text):
                # Six- and eight-digit authenticators are never mixed.
                continue
            for key in record["keys"]:
                until = key["overlap_until"]
                if until is not None and now >= until:
                    continue
                candidates.append((record["id"], key))

        def find_match(lo, hi):
            for drift in range(lo, hi + 1):
                step = current_step + drift
                if step < 0:
                    continue
                for auth_id, key in candidates:
                    token = _hotp(key["secret"], step, len(text))
                    if hmac.compare_digest(token, text):
                        return auth_id, key["key_id"], step, drift
            return None

        match = find_match(-window, window)
        if match is None:
            # Look further out to distinguish genuine drift from a bad code;
            # the tenant window still decides whether it is acceptable.
            distant = find_match(-(window + 10), window + 10)
            if distant is not None:
                return fail("drift_out_of_window", distant[3])
            return fail("invalid_code", self.store["drift"].get((tenant, user), 0))

        auth_id, key_id, matched_step, drift = match
        marker = (auth_id, key_id, matched_step)
        if marker in consumed:
            return fail("replay", drift)

        consumed.add(marker)
        failures[(tenant, user)] = 0
        self.store["drift"][(tenant, user)] = drift
        entry = {
            "tenant": tenant,
            "user": user,
            "ok": True,
            "reason": None,
            "step": matched_step,
            "drift": drift,
            "failures": 0,
        }
        self._record_audit(entry)
        return entry

    def _record_audit(self, entry):
        log = self.store["audit"]
        record = dict(entry)
        record["seq"] = len(log) + 1
        log.append(record)

    def audit(self):
        return [dict(entry) for entry in self.store["audit"]]
