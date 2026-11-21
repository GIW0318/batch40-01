class ScopeAttenuationPlan:
    """Attenuate OAuth-style scopes on refresh and delegation."""

    def __init__(self, hierarchy=None, aliases=None, known_scopes=None,
                 max_scopes=32, delegations=None):
        raise NotImplementedError()

    def normalize(self, scopes):
        raise NotImplementedError()

    def issue(self, user, scopes, audiences, actor=None, now=0):
        raise NotImplementedError()

    def refresh(self, token, scopes=None, now=0):
        raise NotImplementedError()

    def attenuate(self, token, scopes, now=0):
        raise NotImplementedError()

    def delegate(self, token, actor, scopes, audiences):
        raise NotImplementedError()

    def update_user_scopes(self, user, scopes, now=0):
        raise NotImplementedError()

    def check(self, token, audience, now=0, scope=None):
        raise NotImplementedError()
