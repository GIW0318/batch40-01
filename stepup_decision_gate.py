class StepupDecisionGate:
    """Risk-adaptive step-up decision gate for sensitive actions."""

    def __init__(self, policies=None, method_assurance=None, cache_ttl=0,
                 challenge_ttl=60, verification_ttl=120):
        raise NotImplementedError()

    def decide(self, request, now=0):
        raise NotImplementedError()

    def verify(self, challenge_id, method, now=0):
        raise NotImplementedError()

    def trust_device(self, user, device, session_assurance, verification=None, now=0):
        raise NotImplementedError()

    def audit(self):
        raise NotImplementedError()
