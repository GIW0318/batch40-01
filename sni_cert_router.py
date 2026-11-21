class SniCertRouter:
    def __init__(self, default_domain=None):
        self.default_domain = default_domain

    def normalize(self, name):
        raise NotImplementedError()

    def load_batch(self, certs):
        raise NotImplementedError()

    def select(self, sni, alpn=None, now=0.0):
        raise NotImplementedError()

    def issue_ticket(self, domain, now):
        raise NotImplementedError()

    def resume_ticket(self, ticket, sni, now):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
