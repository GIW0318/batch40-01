class NegativeTrustAnchor:
    def __init__(self, anchors=None, nsec3_iterations_limit=100):
        self.anchors = anchors
        self.nsec3_iterations_limit = nsec3_iterations_limit

    def add_anchor(self, zone, key_tags):
        raise NotImplementedError()

    def rotate_anchor(self, zone, new_tags):
        raise NotImplementedError()

    def finalize_rotation(self, zone):
        raise NotImplementedError()

    def trust_anchor(self, zone):
        raise NotImplementedError()

    def validate(self, zone, packet, now=0.0, cd=False):
        raise NotImplementedError()

    def negative_proof(self, response, now=0.0):
        raise NotImplementedError()

    def store(self, name, status, ttl, now):
        raise NotImplementedError()

    def cache(self, name, status, ttl, now):
        raise NotImplementedError()

    def lookup(self, name, now):
        raise NotImplementedError()

    def resolve(self, name, packet, now=0.0):
        raise NotImplementedError()
