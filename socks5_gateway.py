import hmac
import ipaddress
import socket
import struct
import threading


class _Connection:
    def __init__(self, conn_id):
        self.conn_id = conn_id
        self.state = "new"
        self.buffer = bytearray()
        self.method = None
        self.target = None
        self.peer = None
        self.bind_endpoint = None
        self.last_activity = 0
        self.bytes_forwarded = 0


class Socks5Gateway:
    """SOCKS5 gateway with strict handshake, auth and per-connection state."""

    _METHODS = {"noauth": 0x00, "userpass": 0x02}
    _METHOD_NAMES = {value: key for key, value in _METHODS.items()}

    def __init__(self, users=None, allowed_methods=("noauth", "userpass"),
                 connector=None, udp_allow=None, idle_timeout=300,
                 max_bytes=1048576):
        self.users = dict(users or {})
        self.allowed_methods = tuple(allowed_methods)
        self.connector = connector
        self.udp_allow = {
            self._normalize_host(host) for host in (udp_allow or ())
        }
        self.idle_timeout = idle_timeout
        self.max_bytes = max_bytes
        self._connections = {}
        self._lock = threading.RLock()

    def start(self, conn_id, data):
        with self._lock:
            conn = self._connection(conn_id)
            if conn.state == "closed":
                return self._start_result(True, None, conn)
            if conn.state not in ("new", "greeting"):
                return self._start_result(True, None, conn)

            conn.state = "greeting"
            conn.buffer.extend(data)
            if len(conn.buffer) < 2:
                return self._start_result(False, None, conn)

            version = conn.buffer[0]
            method_count = conn.buffer[1]
            if version != 0x05:
                conn.state = "closed"
                conn.buffer.clear()
                return self._start_result(True, b"\x05\xff", conn)

            total = 2 + method_count
            if len(conn.buffer) < total:
                return self._start_result(False, None, conn)

            offered = set(conn.buffer[2:total])
            del conn.buffer[:total]
            method = self._select_method(offered)
            if method is None:
                conn.state = "closed"
                conn.buffer.clear()
                return self._start_result(True, b"\x05\xff", conn)

            conn.method = method
            if method == 0x00:
                conn.state = "ready"
            else:
                conn.state = "auth"
            return self._start_result(
                True, bytes((0x05, method)), conn
            )

    def authenticate(self, conn_id, data):
        with self._lock:
            conn = self._connections.get(conn_id)
            if conn is None or conn.state != "auth":
                if conn is not None:
                    conn.state = "closed"
                return self._auth_result(1, False, conn)

            conn.buffer.extend(data)
            if conn.buffer and conn.buffer[0] != 0x01:
                return self._auth_failure(conn, retryable=False)
            if len(conn.buffer) < 2:
                return self._auth_result(None, True, conn)

            username_length = conn.buffer[1]
            if username_length == 0:
                return self._auth_failure(conn, retryable=False)

            if len(conn.buffer) < 2 + username_length + 1:
                return self._auth_result(None, True, conn)

            password_length_index = 2 + username_length
            password_length = conn.buffer[password_length_index]
            if password_length == 0:
                return self._auth_failure(conn, retryable=False)

            total = password_length_index + 1 + password_length
            if len(conn.buffer) < total:
                return self._auth_result(None, True, conn)

            username_bytes = bytes(conn.buffer[2:password_length_index])
            password_bytes = bytes(conn.buffer[password_length_index + 1:total])
            del conn.buffer[:total]

            try:
                username = username_bytes.decode("utf-8")
                password = password_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return self._auth_failure(conn, retryable=False)

            expected = self.users.get(username)
            valid = expected is not None and hmac.compare_digest(
                password.encode("utf-8"), expected.encode("utf-8")
            )
            if not valid:
                return self._auth_failure(conn, retryable=True)

            conn.state = "ready"
            return self._auth_result(0, False, conn)

    def request(self, conn_id, data, now=0, peer=None):
        with self._lock:
            conn = self._connections.get(conn_id)
            if conn is None or conn.state == "closed":
                return self._request_result(
                    None, "closed", None, None
                )
            if conn.state not in ("ready", "request"):
                return self._request_result(
                    0x01, conn.state, None, None
                )

            conn.state = "request"
            conn.buffer.extend(data)
            parsed = self._parse_request(conn.buffer)
            if parsed is None:
                return self._request_result(None, "request", None, None)
            if isinstance(parsed, int):
                conn.state = "closed"
                conn.buffer.clear()
                return self._request_result(
                    parsed, "closed", None, self._reply(parsed)
                )

            command, target, total = parsed
            del conn.buffer[:total]
            conn.target = target

            if command == 0x01:
                return self._connect(conn, target, now)
            if command == 0x02:
                return self._bind(conn, target, now)
            if command == 0x03:
                return self._associate(conn, target, now, peer)
            return self._request_result(
                0x07, "closed", target, self._reply(0x07)
            )

    def udp_send(self, conn_id, packet, source):
        with self._lock:
            conn = self._connections.get(conn_id)
            if conn is None or conn.state != "udp_associate":
                return self._udp_result(False, "not_associated")
            if conn.peer is None or self._endpoint(source) != conn.peer:
                return self._udp_result(False, "bad_source")
            if not isinstance(packet, dict):
                return self._udp_result(False, "bad_target")

            host = packet.get("host")
            port = packet.get("port")
            if not isinstance(host, str) or not isinstance(port, int):
                return self._udp_result(False, "bad_target")
            if not 0 <= port <= 65535:
                return self._udp_result(False, "bad_target")

            normalized_host = self._normalize_host(host)
            if normalized_host not in self.udp_allow:
                return self._udp_result(False, "target_denied")
            return self._udp_result(True, None)

    def forward(self, conn_id, data, now=0):
        with self._lock:
            conn = self._connections.get(conn_id)
            if conn is None or conn.state != "connected":
                reason = (
                    "closed" if conn is None or conn.state == "closed"
                    else "not_connected"
                )
                return self._forward_result(False, 0, reason)

            if now - conn.last_activity >= self.idle_timeout:
                conn.state = "closed"
                return self._forward_result(False, 0, "idle_timeout")

            amount = len(data)
            if conn.bytes_forwarded + amount > self.max_bytes:
                conn.state = "closed"
                return self._forward_result(False, 0, "byte_limit")

            conn.bytes_forwarded += amount
            conn.last_activity = now
            return self._forward_result(True, amount, None)

    def state(self, conn_id):
        with self._lock:
            conn = self._connections.get(conn_id)
            return conn.state if conn is not None else "closed"

    def _connection(self, conn_id):
        conn = self._connections.get(conn_id)
        if conn is None:
            conn = _Connection(conn_id)
            self._connections[conn_id] = conn
        return conn

    def _select_method(self, offered):
        available = []
        if self.users and "userpass" in self.allowed_methods:
            available.append(0x02)
        if not self.users and "noauth" in self.allowed_methods:
            available.append(0x00)
        for method in available:
            if method in offered:
                return method
        return None

    def _parse_request(self, buffer):
        if len(buffer) < 4:
            return None

        version, command, reserved, address_type = buffer[:4]
        if version != 0x05 or reserved != 0x00:
            return 0x01
        if command not in (0x01, 0x02, 0x03):
            return 0x07

        offset = 4
        if address_type == 0x01:
            if len(buffer) < offset + 4 + 2:
                return None
            host = str(ipaddress.IPv4Address(bytes(buffer[offset:offset + 4])))
            offset += 4
        elif address_type == 0x04:
            if len(buffer) < offset + 16 + 2:
                return None
            host = str(ipaddress.IPv6Address(bytes(buffer[offset:offset + 16])))
            offset += 16
        elif address_type == 0x03:
            if len(buffer) < offset + 1:
                return None
            domain_length = buffer[offset]
            if not 1 <= domain_length <= 255:
                return 0x08
            offset += 1
            if len(buffer) < offset + domain_length + 2:
                return None
            raw_host = bytes(buffer[offset:offset + domain_length])
            offset += domain_length
            try:
                host = raw_host.decode("idna")
            except UnicodeError:
                return 0x08
            host = self._parse_domain_literal(host)
            if host is None:
                return 0x08
        else:
            return 0x08

        port = struct.unpack("!H", buffer[offset:offset + 2])[0]
        offset += 2
        return command, {"host": host, "port": port}, offset

    def _parse_domain_literal(self, host):
        if not host:
            return None
        if host.startswith("[") or host.endswith("]"):
            if not (host.startswith("[") and host.endswith("]")):
                return None
            literal = host[1:-1]
            try:
                return str(ipaddress.IPv6Address(literal))
            except ValueError:
                return None
        return host

    def _connect(self, conn, target, now):
        if self.connector is None:
            conn.state = "closed"
            return self._request_result(
                0x01, "closed", target, self._reply(0x01)
            )

        conn.state = "connecting"
        try:
            result = self.connector(target["host"], target["port"])
        except ConnectionRefusedError:
            code = 0x05
        except (TimeoutError, socket.timeout):
            code = 0x04
        except OSError as exc:
            code = self._error_code(exc)
        except Exception:
            code = 0x01
        else:
            if result:
                conn.state = "connected"
                conn.last_activity = now
                conn.bytes_forwarded = 0
                return self._request_result(
                    0x00, "connected", target, self._reply(0x00)
                )
            code = 0x05

        conn.state = "closed"
        return self._request_result(
            code, "closed", target, self._reply(code)
        )

    def _bind(self, conn, target, now):
        conn.state = "bind_pending"
        conn.bind_endpoint = (target["host"], target["port"])
        conn.last_activity = now
        return self._request_result(
            0x00, "bind_pending", target, self._reply(0x00)
        )

    def _associate(self, conn, target, now, peer):
        conn.state = "udp_associate"
        conn.peer = self._endpoint(peer) if peer is not None else None
        conn.last_activity = now
        return self._request_result(
            0x00, "udp_associate", target, self._reply(0x00)
        )

    def _error_code(self, exc):
        if isinstance(exc, ConnectionRefusedError):
            return 0x05
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return 0x04
        errno = getattr(exc, "errno", None)
        if errno in (100, 101):
            return 0x03
        if errno == 113:
            return 0x04
        if errno == 111:
            return 0x05
        return 0x01

    def _auth_failure(self, conn, retryable):
        conn.buffer.clear()
        if not retryable:
            conn.state = "closed"
        return self._auth_result(1, retryable, conn)

    def _endpoint(self, endpoint):
        if not isinstance(endpoint, (tuple, list)) or len(endpoint) < 2:
            return None
        return (self._normalize_host(endpoint[0]), endpoint[1])

    def _normalize_host(self, host):
        if not isinstance(host, str):
            return host
        if host.startswith("[") and host.endswith("]"):
            return host[1:-1]
        return host

    @staticmethod
    def _reply(code):
        return b"\x05" + bytes((code,)) + b"\x00\x01\x00\x00\x00\x00\x00\x00"

    @staticmethod
    def _start_result(complete, reply, conn):
        return {
            "complete": complete,
            "reply": reply,
            "state": conn.state if conn is not None else "closed",
        }

    @staticmethod
    def _auth_result(status, retryable, conn):
        if status is None:
            reply = None
        else:
            reply = bytes((0x01, status))
        return {
            "status": status,
            "retryable": retryable,
            "reply": reply,
            "state": conn.state if conn is not None else "closed",
        }

    @staticmethod
    def _request_result(code, state, target, reply):
        return {
            "complete": reply is not None,
            "code": code,
            "state": state,
            "target": target,
            "reply": reply,
        }

    @staticmethod
    def _udp_result(accepted, reason):
        return {"accepted": accepted, "reason": reason}

    @staticmethod
    def _forward_result(accepted, amount, reason):
        return {"accepted": accepted, "bytes": amount, "reason": reason}
