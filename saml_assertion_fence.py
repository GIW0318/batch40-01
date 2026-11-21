"""SAML assertion validation fence with strict single-assertion handling."""


class SamlAssertionFence:
    """Validates signed SAML responses against metadata and request state."""

    def __init__(self, entity_id, acs_url, metadata, verifier=None,
                 decryptor=None, allowed_digests=("sha256", "sha384", "sha512")):
        raise NotImplementedError()

    def set_decryption_key(self, receiver, key):
        raise NotImplementedError()

    def process(self, response_xml, request_id, now, receiver=None):
        raise NotImplementedError()

    def logout(self, assertion_id, now=0):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
