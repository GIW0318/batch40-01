class Socks5Gateway:
    """SOCKS5 gateway with strict handshake, auth and per-connection state."""

    def __init__(self, users=None, allowed_methods=("noauth", "userpass"),
                 connector=None, udp_allow=None, idle_timeout=300,
                 max_bytes=1048576):
        self.users = dict(users or {})
        self.allowed_methods = tuple(allowed_methods)
        self.connector = connector
        self.udp_allow = set(udp_allow or ())
        self.idle_timeout = idle_timeout
        self.max_bytes = max_bytes

    def start(self, conn_id, data):
        raise NotImplementedError

    def authenticate(self, conn_id, data):
        raise NotImplementedError

    def request(self, conn_id, data, now=0, peer=None):
        raise NotImplementedError

    def udp_send(self, conn_id, packet, source):
        raise NotImplementedError

    def forward(self, conn_id, data, now=0):
        raise NotImplementedError

    def state(self, conn_id):
        raise NotImplementedError
