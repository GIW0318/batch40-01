import threading
import uuid


DEFAULT_METHOD_ASSURANCE = {
    "password": 1,
    "sms": 2,
    "totp": 3,
    "hardware": 4,
}


class StepupDecisionGate:
    """Risk-adaptive step-up decision gate for sensitive actions."""

    def __init__(self, policies=None, method_assurance=None, cache_ttl=0,
                 challenge_ttl=60, verification_ttl=120):
        self._policies = dict(policies or {})
        self._method_assurance = dict(method_assurance or DEFAULT_METHOD_ASSURANCE)
        self._cache_ttl = cache_ttl
        self._challenge_ttl = challenge_ttl
        self._verification_ttl = verification_ttl
        self._challenges = {}
        self._challenge_index = {}
        self._verifications = {}
        self._grace = {}
        self._cache = {}
        self._trusted_devices = set()
        self._audit = []
        self._lock = threading.Lock()

    def decide(self, request, now=0):
        with self._lock:
            return self._decide(request, now)

    def _decide(self, request, now):
        user = request["user"]
        action = request["action"]
        resource = request["resource"]
        policy = self._policies.get(action)

        if policy is None:
            return self._finish("deny", "no policy", request, now)

        if not request.get("risk_available", True):
            return self._finish("deny", "risk unavailable", request, now)

        verification = request.get("verification")
        if verification is not None:
            return self._decide_with_verification(verification, request, policy, now)

        if self._cache_ttl > 0:
            cached = self._cache.get((user, action, resource))
            if cached is not None and now < cached["expires_at"]:
                hit = dict(cached["decision"])
                hit["cached"] = True
                self._record("allow", request, now, reason="cache")
                return hit

        grace_expiry = self._grace.get((user, action, resource))
        if (grace_expiry is not None and policy["grace_seconds"] > 0
                and now < grace_expiry):
            return self._finish("allow", "grace", request, now,
                                expires_at=grace_expiry)

        risk_score = request.get("risk_score", 0)
        assurance = request.get("session_assurance", 0)
        if risk_score >= policy["risk_threshold"] or assurance < policy["min_assurance"]:
            challenge = self._challenge_for(user, action, resource, now)
            decision = self._finish(
                "stepup", "policy", request, now,
                required_assurance=policy["min_assurance"],
                challenge_id=challenge["id"],
                expires_at=challenge["expires_at"],
            )
            return decision

        decision = self._finish("allow", "policy", request, now)
        if self._cache_ttl > 0:
            self._cache[(user, action, resource)] = {
                "decision": decision,
                "expires_at": now + self._cache_ttl,
            }
        return decision

    def _decide_with_verification(self, token, request, policy, now):
        record = self._verifications.get(token)
        if record is None:
            return self._finish("deny", "verification invalid", request, now)
        if record["used"]:
            return self._finish("deny", "verification already used", request, now)
        if now >= record["expires_at"]:
            return self._finish("deny", "verification expired", request, now)
        if (record["user"] != request["user"]
                or record["action"] != request["action"]
                or record["resource"] != request["resource"]):
            return self._finish("deny", "verification mismatch", request, now)

        record["used"] = True
        grace_seconds = policy["grace_seconds"]
        expires_at = None
        if grace_seconds > 0:
            expires_at = now + grace_seconds
            key = (request["user"], request["action"], request["resource"])
            self._grace[key] = expires_at
        return self._finish("allow", "verification", request, now,
                            expires_at=expires_at)

    def verify(self, challenge_id, method, now=0):
        with self._lock:
            challenge = self._challenges.get(challenge_id)
            if challenge is None or now >= challenge["expires_at"]:
                return {"verified": False, "reason": "challenge expired"}

            method_level = self._method_assurance.get(method, 0)
            if method_level < challenge["required_assurance"]:
                self._audit.append({
                    "outcome": "downgrade",
                    "user": challenge["user"],
                    "action": challenge["action"],
                    "resource": challenge["resource"],
                    "method": method,
                    "now": now,
                })
                return {"verified": False, "reason": "method too weak"}

            token = uuid.uuid4().hex
            self._verifications[token] = {
                "user": challenge["user"],
                "action": challenge["action"],
                "resource": challenge["resource"],
                "method": method,
                "assurance": method_level,
                "used": False,
                "expires_at": now + self._verification_ttl,
            }
            self._audit.append({
                "outcome": "verified",
                "user": challenge["user"],
                "action": challenge["action"],
                "resource": challenge["resource"],
                "method": method,
                "now": now,
            })
            return {"verified": True, "verification": token}

    def trust_device(self, user, device, session_assurance, verification=None, now=0):
        with self._lock:
            policy = self._policies.get("device.trust", {"min_assurance": 3})
            if session_assurance < policy["min_assurance"]:
                self._audit.append({
                    "outcome": "deny",
                    "user": user,
                    "action": "device.trust",
                    "resource": device,
                    "reason": "insufficient assurance",
                    "now": now,
                })
                return {"trusted": False, "reason": "insufficient assurance"}

            record = self._verifications.get(verification) if verification else None
            if (record is None or record["used"]
                    or now >= record["expires_at"]
                    or record["user"] != user
                    or record["action"] != "device.trust"
                    or record["resource"] != device):
                self._audit.append({
                    "outcome": "deny",
                    "user": user,
                    "action": "device.trust",
                    "resource": device,
                    "reason": "verification required",
                    "now": now,
                })
                return {"trusted": False, "reason": "verification required"}

            record["used"] = True
            self._trusted_devices.add((user, device))
            self._audit.append({
                "outcome": "allow",
                "user": user,
                "action": "device.trust",
                "resource": device,
                "now": now,
            })
            return {"trusted": True}

    def audit(self):
        return list(self._audit)

    def _challenge_for(self, user, action, resource, now):
        key = (user, action, resource)
        existing_id = self._challenge_index.get(key)
        if existing_id is not None:
            existing = self._challenges.get(existing_id)
            if existing is not None and now < existing["expires_at"]:
                return existing
        policy = self._policies[action]
        challenge = {
            "id": uuid.uuid4().hex,
            "user": user,
            "action": action,
            "resource": resource,
            "required_assurance": policy["min_assurance"],
            "expires_at": now + self._challenge_ttl,
        }
        self._challenges[challenge["id"]] = challenge
        self._challenge_index[key] = challenge["id"]
        return challenge

    def _finish(self, outcome, reason, request, now, **extra):
        decision = {
            "decision": outcome,
            "reason": reason,
            "rule": request["action"],
            "expires_at": extra.pop("expires_at", None),
        }
        decision.update(extra)
        self._record(outcome, request, now, reason=reason)
        return decision

    def _record(self, outcome, request, now, reason=None):
        self._audit.append({
            "outcome": outcome,
            "user": request["user"],
            "action": request["action"],
            "resource": request["resource"],
            "reason": reason,
            "now": now,
        })
