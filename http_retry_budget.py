class RetryBudgetPlanner:
    def __init__(self, budget_ratio=0.1, base_delay=0.1, max_delay=2.0,
                 max_attempts=3, max_redirects=3):
        self.budget_ratio = budget_ratio
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_attempts = max_attempts
        self.max_redirects = max_redirects

    def note_request(self, caller):
        raise NotImplementedError()

    def note_retry(self, caller):
        raise NotImplementedError()

    def stats(self):
        raise NotImplementedError()

    def plan(self, request, response, attempt, now):
        raise NotImplementedError()

    def probe_allowed(self, breaker_id, state, now):
        raise NotImplementedError()

    def probe_finished(self, breaker_id):
        raise NotImplementedError()
