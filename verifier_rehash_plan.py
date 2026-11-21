class VerifierRehashPlan:
    """Lazy password hash upgrade planning on the successful login path."""

    def __init__(self, target=None, peppers=None, current_pepper=None,
                 max_password_bytes=1024):
        raise NotImplementedError()

    @staticmethod
    def hash_password(password, algo="argon2id", params=None, pepper_version=1,
                      version=1, salt=None, pepper_secret=None):
        raise NotImplementedError()

    def parse(self, stored):
        raise NotImplementedError()

    def register(self, user, stored):
        raise NotImplementedError()

    def record(self, user):
        raise NotImplementedError()

    def login(self, user, password, now=0):
        raise NotImplementedError()

    def pending(self):
        raise NotImplementedError()

    def plan(self):
        raise NotImplementedError()

    def drain(self):
        raise NotImplementedError()

    def report(self):
        raise NotImplementedError()

    def stats(self):
        raise NotImplementedError()
