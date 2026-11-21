class LandlockRulesetFence:
    """Userspace model of Landlock ruleset construction and enforcement."""

    def __init__(self, abi=3, max_path=4096, symlinks=None, mounts=None):
        raise NotImplementedError()

    def rights_for_abi(self, abi=None):
        raise NotImplementedError()

    def create_ruleset(self, rights, abi=None):
        raise NotImplementedError()

    def add_rule(self, ruleset_id, path, access, kind="dir", allow=True):
        raise NotImplementedError()

    def restrict(self, ruleset_id, pid):
        raise NotImplementedError()

    def spawn(self, parent_pid, child_pid):
        raise NotImplementedError()

    def check(self, pid, path, access):
        raise NotImplementedError()

    def explain(self, pid, path, access):
        raise NotImplementedError()

    def resolve_path(self, path, cwd="/"):
        raise NotImplementedError()
