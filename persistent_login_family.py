class PersistentLoginFamily:
    """Server-side persistent login token family with rotation and reuse detection."""

    def __init__(self, sliding_seconds=2592000, max_lifetime_seconds=7776000,
                 transition_seconds=5, token_factory=None):
        raise NotImplementedError()

    def issue(self, user, device, now=0):
        raise NotImplementedError()

    def validate(self, token, device, now=0, user=None):
        raise NotImplementedError()

    def rotate(self, token, device, now=0, user=None):
        raise NotImplementedError()

    def logout(self, user, family_id=None):
        raise NotImplementedError()

    def change_password(self, user, now=0):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
