import os
import zlib


OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

CONTROL_OPCODES = frozenset((OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG))
VALID_OPCODES = frozenset(
    (
        OPCODE_CONTINUATION,
        OPCODE_TEXT,
        OPCODE_BINARY,
        OPCODE_CLOSE,
        OPCODE_PING,
        OPCODE_PONG,
    )
)

RESERVED_CLOSE_CODES = frozenset((1004, 1005, 1006, 1012, 1013, 1014))
CONTROL_FRAME_MAX_PAYLOAD = 125
DEFLATE_TAIL = b"\x00\x00\xff\xff"

FRAME_TYPES = {
    OPCODE_CONTINUATION: "continuation",
    OPCODE_TEXT: "text",
    OPCODE_BINARY: "binary",
    OPCODE_CLOSE: "close",
    OPCODE_PING: "ping",
    OPCODE_PONG: "pong",
}


def _close_code_allowed(code):
    if code in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1015):
        return True
    if 3000 <= code <= 3999 or 4000 <= code <= 4999:
        return True
    return False


class WsFrameAudit:
    """Incremental WebSocket frame auditor for one or more connections."""

    def __init__(self, max_message=1048576, extensions=(), compression=False):
        self.max_message = max_message
        self.extensions = tuple(extensions)
        self.compression = compression
        self._connections = {}
        self._records = []

    def feed(self, conn_id, data, direction="client"):
        result = {"records": [], "messages": [], "out": None}
        conn = self._connections.setdefault(
            conn_id,
            {
                "buffer": b"",
                "state": "open",
                "frag_buf": bytearray(),
                "frag_opcode": None,
                "frag_len": 0,
                "compressed": False,
                "decompressor": None,
            },
        )
        conn["buffer"] += bytes(data)

        while conn["buffer"] and conn["state"] != "closed":
            frame = self._take_frame(conn)
            if frame is None:
                break
            if frame.get("fatal_header"):
                record = self._record(
                    conn_id,
                    FRAME_TYPES.get(frame["opcode"], "invalid"),
                    frame["length"],
                    "reject",
                    frame["fatal_header"],
                )
                result["records"].append(record)
                conn["state"] = "closed"
                conn["buffer"] = b""
                break
            self._handle_frame(conn_id, conn, frame, direction, result)

        return result

    def audit(self):
        return list(self._records)

    def state(self, conn_id):
        conn = self._connections.get(conn_id)
        if conn is None:
            return "open"
        return conn["state"]

    def _take_frame(self, conn):
        """Extract one complete frame from the incremental buffer."""
        buffer = conn["buffer"]
        if len(buffer) < 2:
            return None

        first_byte, second_byte = buffer[0], buffer[1]
        fin = bool(first_byte & 0x80)
        rsv = (first_byte >> 4) & 0x07
        opcode = first_byte & 0x0F
        masked = bool(second_byte & 0x80)
        length_code = second_byte & 0x7F

        offset = 2
        if length_code == 126:
            if len(buffer) < offset + 2:
                return None
            length = int.from_bytes(buffer[offset:offset + 2], "big")
            offset += 2
        elif length_code == 127:
            if len(buffer) < offset + 8:
                return None
            extended = buffer[offset:offset + 8]
            if extended[0] & 0x80:
                conn["buffer"] = b""
                return {
                    "opcode": opcode,
                    "length": int.from_bytes(extended, "big"),
                    "fatal_header": "invalid_64bit_length",
                }
            length = int.from_bytes(extended, "big")
            offset += 8
        else:
            length = length_code

        if masked:
            if len(buffer) < offset + 4:
                return None
            masking_key = buffer[offset:offset + 4]
            offset += 4
        else:
            masking_key = None

        if len(buffer) < offset + length:
            return None

        payload = bytearray(buffer[offset:offset + length])
        if masked:
            for index in range(len(payload)):
                payload[index] ^= masking_key[index % 4]

        conn["buffer"] = buffer[offset + length:]
        return {
            "fin": fin,
            "rsv": rsv,
            "opcode": opcode,
            "masked": masked,
            "length": length,
            "payload": bytes(payload),
        }

    def _handle_frame(self, conn_id, conn, frame, direction, result):
        opcode = frame["opcode"]
        payload = frame["payload"]
        is_control = opcode in CONTROL_OPCODES

        reason = self._validate_frame(conn, frame, is_control, direction)
        if reason is None:
            if is_control:
                self._handle_control(conn_id, conn, frame, direction, result)
            else:
                reason = self._handle_data(conn_id, conn, frame, result)

        verdict = "reject" if reason is not None else "ok"
        record = self._record(
            conn_id, FRAME_TYPES.get(opcode, "invalid"),
            frame["length"], verdict, reason,
        )
        result["records"].append(record)

        if reason is not None:
            conn["state"] = "closed"
            conn["frag_buf"] = bytearray()
            conn["frag_opcode"] = None
            conn["frag_len"] = 0

    def _validate_frame(self, conn, frame, is_control, direction):
        if not self._rsv_allowed(frame["rsv"], frame["opcode"]):
            return "rsv_without_extension"
        if frame["masked"] and direction == "server":
            return "mask_forbidden"
        if not frame["masked"] and direction == "client":
            return "mask_required"
        if frame["opcode"] not in VALID_OPCODES:
            return "invalid_opcode"
        if is_control:
            if not frame["fin"]:
                return "fragmented_control"
            if frame["length"] > CONTROL_FRAME_MAX_PAYLOAD:
                return "control_frame_too_long"
            if frame["opcode"] == OPCODE_CLOSE:
                return self._validate_close(frame["payload"])
            return None

        if frame["opcode"] == OPCODE_CONTINUATION:
            if conn["frag_opcode"] is None:
                return "unexpected_continuation"
        elif conn["frag_opcode"] is not None:
            return "expected_continuation"

        if conn["frag_len"] + frame["length"] > self.max_message:
            return "message_too_large"
        return None

    def _rsv_allowed(self, rsv, opcode):
        if rsv == 0:
            return True
        negotiated = self.compression or "permessage-deflate" in self.extensions
        if rsv == 0x4 and negotiated and opcode != OPCODE_CONTINUATION:
            return True
        return False

    @staticmethod
    def _validate_close(payload):
        if len(payload) == 0:
            return None
        if len(payload) == 1:
            return "invalid_close_payload"
        code = int.from_bytes(payload[:2], "big")
        if code in RESERVED_CLOSE_CODES or not _close_code_allowed(code):
            return "reserved_close_code"
        try:
            payload[2:].decode("utf-8")
        except UnicodeDecodeError:
            return "invalid_utf8"
        return None

    def _handle_control(self, conn_id, conn, frame, direction, result):
        opcode = frame["opcode"]
        if opcode == OPCODE_PING:
            result["out"] = self._build_frame(
                OPCODE_PONG, frame["payload"], direction
            )
        elif opcode == OPCODE_CLOSE:
            if conn["state"] == "open":
                result["out"] = self._build_frame(
                    OPCODE_CLOSE, frame["payload"], direction
                )
                conn["state"] = "closing"
            else:
                conn["state"] = "closed"

    def _handle_data(self, conn_id, conn, frame, result):
        if frame["opcode"] in (OPCODE_TEXT, OPCODE_BINARY):
            conn["frag_opcode"] = frame["opcode"]
            conn["frag_len"] = 0
            conn["compressed"] = bool(frame["rsv"] & 0x4)
            if conn["compressed"] and conn["decompressor"] is None:
                conn["decompressor"] = zlib.decompressobj(-15)

        payload = frame["payload"]
        if conn["compressed"]:
            try:
                payload = self._inflate(conn, payload, frame["fin"])
            except zlib.error:
                return "decompression_failed"

        conn["frag_buf"].extend(payload)
        conn["frag_len"] += len(payload)

        if not frame["fin"]:
            return

        message = bytes(conn["frag_buf"])
        opcode = conn["frag_opcode"]
        conn["frag_buf"] = bytearray()
        conn["frag_opcode"] = None
        conn["frag_len"] = 0
        conn["compressed"] = False

        if opcode == OPCODE_TEXT:
            try:
                decoded = message.decode("utf-8")
            except UnicodeDecodeError:
                return "invalid_utf8"
            result["messages"].append(decoded)
        else:
            result["messages"].append(message)
        return None

    @staticmethod
    def _inflate(conn, payload, final):
        decompressor = conn["decompressor"]
        inflated = decompressor.decompress(payload)
        if final and not payload.endswith(DEFLATE_TAIL):
            inflated += decompressor.decompress(DEFLATE_TAIL)
        return inflated

    def _build_frame(self, opcode, payload, peer_direction):
        first_byte = 0x80 | opcode
        length = len(payload)
        mask = peer_direction == "server"
        second_byte = (0x80 if mask else 0x00) | length
        frame = bytes((first_byte, second_byte))
        if mask:
            masking_key = os.urandom(4)
            frame += masking_key
            frame += bytes(
                byte ^ masking_key[index % 4]
                for index, byte in enumerate(payload)
            )
        else:
            frame += payload
        return frame

    def _record(self, conn_id, frame_type, length, verdict, reason):
        record = {
            "conn": conn_id,
            "type": frame_type,
            "length": length,
            "verdict": verdict,
            "reason": reason,
        }
        self._records.append(record)
        return record
