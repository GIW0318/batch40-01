class CgroupLimitGuard:
    """Validate and apply cgroup v2 limits across a group tree."""

    def __init__(self, root="/sys/fs/cgroup", max_name_length=64):
        raise NotImplementedError()

    def create_group(self, path, parent=None, limits=None, controllers=None):
        raise NotImplementedError()

    def get(self, path):
        raise NotImplementedError()

    def set_usage(self, path, memory=None, pids=None):
        raise NotImplementedError()

    def parse_limit(self, key, value):
        raise NotImplementedError()

    def set_limits(self, path, limits):
        raise NotImplementedError()

    def set_many(self, changes):
        raise NotImplementedError()

    def delegate(self, path, owner, controllers):
        raise NotImplementedError()

    def stats(self, path):
        raise NotImplementedError()

    def can_fork(self, path, count=1):
        raise NotImplementedError()
