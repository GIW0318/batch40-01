class VersionConflict(Exception):
    """Raised when a SCIM resource is updated with a stale version."""


class ScimDeprovisionSync:
    """SCIM 2.0 user/group sync with deprovision propagation."""

    def __init__(self, page_size=2):
        raise NotImplementedError()

    def create_user(self, payload):
        raise NotImplementedError()

    def get_user(self, user_id):
        raise NotImplementedError()

    def find_user(self, filter_expr):
        raise NotImplementedError()

    def list_users(self, start_index=1, count=None):
        raise NotImplementedError()

    def update_user(self, user_id, payload, version=None):
        raise NotImplementedError()

    def patch_user(self, user_id, operations, version=None):
        raise NotImplementedError()

    def deactivate_user(self, user_id):
        raise NotImplementedError()

    def delete_user(self, user_id):
        raise NotImplementedError()

    def create_group(self, payload):
        raise NotImplementedError()

    def add_member(self, group_id, user_id):
        raise NotImplementedError()

    def remove_member(self, group_id, user_id):
        raise NotImplementedError()

    def patch_group(self, group_id, operations, version=None):
        raise NotImplementedError()

    def effective_groups(self, user_id):
        raise NotImplementedError()

    def create_session(self, user_id, now=0):
        raise NotImplementedError()

    def sessions(self, user_id):
        raise NotImplementedError()

    def tokens(self, user_id):
        raise NotImplementedError()

    def bulk(self, operations):
        raise NotImplementedError()
