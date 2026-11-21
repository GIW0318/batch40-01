class SeccompProfileMerge:
    """Parse and intersect seccomp profiles across architectures."""

    def __init__(self, known_syscalls=None):
        raise NotImplementedError()

    def parse(self, text, includes=None):
        raise NotImplementedError()

    def merge(self, base, overlay, allow_notify=False):
        raise NotImplementedError()

    def decide(self, profile, arch, syscall, args=None):
        raise NotImplementedError()
