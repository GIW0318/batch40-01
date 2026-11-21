class WsFrameAudit:
    """Incremental WebSocket frame auditor for one or more connections."""

    def __init__(self, max_message=1048576, extensions=(), compression=False):
        self.max_message = max_message
        self.extensions = tuple(extensions)
        self.compression = compression

    def feed(self, conn_id, data, direction="client"):
        raise NotImplementedError

    def audit(self):
        raise NotImplementedError

    def state(self, conn_id):
        raise NotImplementedError
