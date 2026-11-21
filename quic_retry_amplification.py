class QuicRetryGuard:
    def __init__(self, secret=b"retry-secret", max_amplification=3,
                 token_ttl=10.0, retry_prefix="c0ffee"):
        self.secret = secret
        self.max_amplification = max_amplification
        self.token_ttl = token_ttl
        self.retry_prefix = retry_prefix

    def rotate_key(self):
        raise NotImplementedError()

    def issue_retry(self, connection_id, address, now):
        raise NotImplementedError()

    def validate_token(self, token, address, connection_id, now):
        raise NotImplementedError()

    def check_initial(self, connection_id, address, size, now, token=None):
        raise NotImplementedError()

    def send_budget(self, address):
        raise NotImplementedError()

    def charge_send(self, address, size):
        raise NotImplementedError()

    def on_migration(self, connection_id, old_address, new_address, now):
        raise NotImplementedError()

    def validate_path_challenge(self, connection_id, challenge, address, now):
        raise NotImplementedError()

    def on_0rtt(self, connection_id, ticket, now):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
