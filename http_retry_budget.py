import random


class RetryBudgetPlanner:
    IDEMPOTENT_METHODS = frozenset(
        {"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"}
    )
    RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

    def __init__(self, budget_ratio=0.1, base_delay=0.1, max_delay=2.0,
                 max_attempts=3, max_redirects=3):
        self.budget_ratio = budget_ratio
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_attempts = max_attempts
        self.max_redirects = max_redirects
        self._caller_stats = {}
        self._half_open_probes = set()

    def _caller(self, caller):
        if caller not in self._caller_stats:
            self._caller_stats[caller] = {"requests": 0, "retries": 0}
        return self._caller_stats[caller]

    def note_request(self, caller):
        self._caller(caller)["requests"] += 1

    def note_retry(self, caller):
        stats = self._caller(caller)
        budget = stats["requests"] * self.budget_ratio
        if stats["retries"] >= budget:
            return False
        stats["retries"] += 1
        return True

    def stats(self):
        requests = sum(item["requests"] for item in self._caller_stats.values())
        retries = sum(item["retries"] for item in self._caller_stats.values())
        return {
            "requests": requests,
            "retries": retries,
            "attempts": requests + retries,
            "per_caller": {
                caller: dict(item) for caller, item in self._caller_stats.items()
            },
        }

    def plan(self, request, response, attempt, now):
        if response.get("committed"):
            return {"retry": False, "reason": "committed"}

        if attempt >= self.max_attempts:
            return {"retry": False, "reason": "max_attempts"}

        status = response["status"]
        if 300 <= status < 400:
            if request.get("redirects", 0) >= self.max_redirects:
                return {"retry": False, "reason": "redirect_limit"}
            return {"retry": False, "reason": "redirect"}

        method = request["method"].upper()
        method_idempotent = method in self.IDEMPOTENT_METHODS or (
            method == "POST" and request.get("idempotency_key") is not None
        )
        if not method_idempotent:
            return {"retry": False, "reason": "not_idempotent"}

        if not request.get("body_replayable", True):
            return {"retry": False, "reason": "body_not_replayable"}

        if status not in self.RETRYABLE_STATUSES:
            return {"retry": False, "reason": "status"}

        retry_after = response.get("retry_after")
        if retry_after is not None:
            delay = float(retry_after)
        else:
            backoff = self.base_delay * (2 ** (attempt - 1))
            delay = min(self.max_delay, backoff * (1.0 + random.random() * 0.5))

        deadline = request.get("deadline")
        if deadline is not None and now + delay > deadline:
            return {"retry": False, "reason": "deadline"}

        return {"retry": True, "delay": delay}

    def probe_allowed(self, breaker_id, state, now):
        if state != "half_open":
            return state == "closed"
        if breaker_id in self._half_open_probes:
            return False
        self._half_open_probes.add(breaker_id)
        return True

    def probe_finished(self, breaker_id):
        self._half_open_probes.discard(breaker_id)
