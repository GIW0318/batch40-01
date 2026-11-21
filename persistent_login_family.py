import hashlib
import hmac
import os


def _constant_time_equals(left, right):
    """Compare strings/bytes in constant time without leaking length either."""
    if isinstance(left, str):
        left = left.encode("utf-8")
    if isinstance(right, str):
        right = right.encode("utf-8")
    return hmac.compare_digest(left, right)


def _digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token():
    return hashlib.sha256(os.urandom(32)).hexdigest()


def _new_family_id():
    return os.urandom(16).hex()


class PersistentLoginFamily:
    """Server-side persistent login token family with rotation and reuse detection."""

    def __init__(self, sliding_seconds=2592000, max_lifetime_seconds=7776000,
                 transition_seconds=5, token_factory=None):
        self.sliding_seconds = sliding_seconds
        self.max_lifetime_seconds = max_lifetime_seconds
        self.transition_seconds = transition_seconds
        self.token_factory = token_factory or _new_token
        self._families = []
        self._digests = set()

    def _mint_token(self):
        token = self.token_factory()
        while _digest(token) in self._digests:
            token = self.token_factory()
        return token

    def _mint_family_id(self):
        family_id = _new_family_id()
        existing = {family["family_id"] for family in self._families}
        while family_id in existing:
            family_id = _new_family_id()
        return family_id

    def _find_family(self, token):
        digest = _digest(token)
        for family in self._families:
            if _constant_time_equals(family["digest"], digest):
                return family
            if family["previous_digest"] is not None and _constant_time_equals(
                    family["previous_digest"], digest):
                return family
        return None

    def issue(self, user, device, now=0):
        token = self._mint_token()
        family = {
            "family_id": self._mint_family_id(),
            "user": user,
            "device": device,
            "digest": _digest(token),
            "previous_digest": None,
            "grace_until": None,
            "created_at": now,
            "last_used": now,
            "revoked": False,
        }
        self._families.append(family)
        self._digests.add(family["digest"])
        return {"token": token, "family_id": family["family_id"]}

    def _reuse_detected(self, family):
        family["revoked"] = True

    def _expired(self, family, now):
        if now - family["created_at"] >= self.max_lifetime_seconds:
            return True
        if now - family["last_used"] >= self.sliding_seconds:
            return True
        return False

    def _is_grace_use(self, family, digest, now):
        return (
            family["previous_digest"] is not None
            and family["grace_until"] is not None
            and now <= family["grace_until"]
            and _constant_time_equals(family["previous_digest"], digest)
        )

    def validate(self, token, device, now=0, user=None):
        family = self._find_family(token)
        if family is None:
            return {"valid": False, "reason": "unknown token"}

        digest = _digest(token)
        is_current = _constant_time_equals(family["digest"], digest)

        if not is_current and not self._is_grace_use(family, digest, now):
            self._reuse_detected(family)
            return {"valid": False, "reason": "theft",
                    "family_id": family["family_id"]}

        if family["revoked"]:
            return {"valid": False, "reason": "revoked",
                    "family_id": family["family_id"]}

        if self._expired(family, now):
            return {"valid": False, "reason": "expired",
                    "family_id": family["family_id"]}

        if user is not None and not _constant_time_equals(family["user"], user):
            return {"valid": False, "reason": "wrong user",
                    "family_id": family["family_id"]}

        if not _constant_time_equals(family["device"], device):
            return {"valid": False, "reason": "device mismatch",
                    "family_id": family["family_id"]}

        family["last_used"] = now
        return {"valid": True, "family_id": family["family_id"],
                "user": family["user"]}

    def rotate(self, token, device, now=0, user=None):
        family = self._find_family(token)
        if family is None:
            return {"status": "unknown"}

        digest = _digest(token)
        is_current = _constant_time_equals(family["digest"], digest)

        if not is_current:
            if not self._is_grace_use(family, digest, now):
                self._reuse_detected(family)
                return {"status": "theft", "family_id": family["family_id"]}
            return {"status": "grace", "token": token,
                    "family_id": family["family_id"]}

        if family["revoked"]:
            return {"status": "revoked", "family_id": family["family_id"]}

        if self._expired(family, now):
            return {"status": "expired", "family_id": family["family_id"]}

        if user is not None and not _constant_time_equals(family["user"], user):
            return {"status": "wrong user", "family_id": family["family_id"]}

        if not _constant_time_equals(family["device"], device):
            return {"status": "device mismatch",
                    "family_id": family["family_id"]}

        new_token = self._mint_token()
        old_digest = family["digest"]
        self._digests.discard(old_digest)
        family["previous_digest"] = old_digest
        family["digest"] = _digest(new_token)
        family["grace_until"] = now + self.transition_seconds
        family["last_used"] = now
        self._digests.add(family["digest"])
        return {"status": "rotated", "token": new_token,
                "family_id": family["family_id"]}

    def logout(self, user, family_id=None):
        revoked = 0
        for family in self._families:
            if not _constant_time_equals(family["user"], user):
                continue
            if family["revoked"]:
                continue
            if family_id is not None and not _constant_time_equals(
                    family["family_id"], family_id):
                continue
            family["revoked"] = True
            revoked += 1
        return {"revoked": revoked}

    def change_password(self, user, now=0):
        result = self.logout(user)
        return {"revoked_families": result["revoked"]}

    def snapshot(self):
        digests = sorted(
            family["digest"]
            for family in self._families
            if not family["revoked"]
        )
        return {"digests": digests}
