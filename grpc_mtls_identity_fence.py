class MtlsIdentityFence:
    def __init__(self, trust_domain="cluster.local"):
        self.trust_domain = trust_domain

    def on_rotation(self, generation, valid_serials):
        raise NotImplementedError()

    def revoke(self, serial):
        raise NotImplementedError()

    def check_peer(self, peer, now=0.0):
        raise NotImplementedError()

    def authorize(self, identity, policy, method, path, now=0.0):
        raise NotImplementedError()

    def bind_connection(self, connection_id, identity):
        raise NotImplementedError()

    def state_dump(self):
        raise NotImplementedError()
