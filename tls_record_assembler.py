"""Incremental TLS record assembler used by the auditing proxy."""


CT_CHANGE_CIPHER_SPEC = 20
CT_ALERT = 21
CT_HANDSHAKE = 22
CT_APPLICATION_DATA = 23
CT_HEARTBEAT = 24

HT_CLIENT_HELLO = 1
HT_SERVER_HELLO = 2
HT_FINISHED = 20


class RecordLayerError(Exception):
    """A classified record layer failure."""

    def __init__(self, kind, fatal=True, detail=""):
        super().__init__(kind)
        self.kind = kind
        self.fatal = fatal
        self.detail = detail


class TlsRecordAssembler:
    """Buffers TCP bytes and emits complete TLS records and handshake messages."""

    MAX_RECORD_LEN = 16384
    HEADER_LEN = 5

    _ERROR_KINDS = (
        "record_overflow",
        "empty_record_flood",
        "ccs_flood",
        "unexpected_change_cipher_spec",
        "application_data_before_handshake",
        "application_data_after_close",
        "handshake_incomplete",
        "decrypt_fatal",
        "decrypt_discard",
        "unexpected_handshake",
        "unknown_content_type",
        "buffer_overflow",
        "connection_closed",
    )

    _DISCARD_DECRYPT_REASONS = frozenset((
        "unknown_content_type",
        "unknown_record_type",
        "unexpected_message",
        "discard",
        "ignored",
        "empty_record",
    ))

    def __init__(self, negotiated_version=0x0303, max_record_len=16384,
                 max_empty_records=4, max_ccs_records=8, max_buffer_bytes=65536):
        self.negotiated_version = negotiated_version
        self.max_record_len = min(int(max_record_len), self.MAX_RECORD_LEN)
        self.max_empty_records = int(max_empty_records)
        self.max_ccs_records = int(max_ccs_records)
        self.max_buffer_bytes = int(max_buffer_bytes)

        self._buf = bytearray()
        self._pos = 0
        self._handshake_buf = bytearray()

        self._state = "handshake"
        self._handshake_started = False
        self._handshake_messages = 0
        self._seen_client_hello = False
        self._seen_server_hello = False

        self._records_in = 0
        self._records_out = 0
        self._empty_records = 0
        self._ccs_records = 0
        self._warnings = 0
        self._warning_details = []
        self._bytes_in = 0
        self._bytes_out = 0
        self._errors = {kind: 0 for kind in self._ERROR_KINDS}

    def feed(self, data):
        """Append *data* and return events for every complete record available."""
        if not data:
            return []

        data = bytes(data)
        self._bytes_in += len(data)
        self._buf.extend(data)
        events = []

        while True:
            available = len(self._buf) - self._pos
            if available < self.HEADER_LEN:
                break

            start = self._pos
            content_type = self._buf[start]
            version = int.from_bytes(self._buf[start + 1:start + 3], "big")
            payload_len = int.from_bytes(self._buf[start + 3:start + 5], "big")

            if payload_len > self.max_record_len:
                self._fail(
                    "record_overflow",
                    detail=(
                        "record length %d exceeds limit %d"
                        % (payload_len, self.max_record_len)
                    ),
                )

            if available < self.HEADER_LEN + payload_len:
                break

            payload_start = start + self.HEADER_LEN
            payload_end = payload_start + payload_len
            payload = bytes(self._buf[payload_start:payload_end])
            self._pos = payload_end
            self._records_in += 1

            event = self._handle_record(content_type, version, payload)
            if event is not None:
                events.append(event)
                self._records_out += 1
                self._bytes_out += len(event.get("payload", b""))

        self._auto_compact()
        pending = self.pending()
        if pending > self.max_buffer_bytes:
            self._fail(
                "buffer_overflow",
                detail="pending buffer %d exceeds limit %d"
                       % (pending, self.max_buffer_bytes),
            )
        return events

    def pending(self):
        """Return the number of unparsed bytes currently buffered."""
        return len(self._buf) - self._pos

    def compact(self):
        """Drop consumed bytes without ever discarding a partial record."""
        self._auto_compact()
        return self.pending()

    def on_decrypt_failure(self, reason):
        """Classify a decrypt-stage failure as discardable or fatal."""
        reason = str(reason)
        if reason in self._DISCARD_DECRYPT_REASONS:
            self._errors["decrypt_discard"] += 1
            return {
                "reason": reason,
                "action": "discard",
                "fatal": False,
            }

        self._errors["decrypt_fatal"] += 1
        self._state = "closed"
        return {
            "reason": reason,
            "action": "fatal",
            "fatal": True,
        }

    def errors(self):
        """Return a copy of record-layer error counters."""
        return dict(self._errors)

    def audit(self):
        """Return audit metadata for the current session."""
        return {
            "negotiated_version": self.negotiated_version,
            "state": self._state,
            "records_in": self._records_in,
            "records_out": self._records_out,
            "handshake_messages": self._handshake_messages,
            "empty_records": self._empty_records,
            "ccs_records": self._ccs_records,
            "warnings": self._warnings,
            "warning_details": list(self._warning_details),
            "bytes_in": self._bytes_in,
            "bytes_out": self._bytes_out,
            "pending_bytes": self.pending(),
            "errors": dict(self._errors),
        }

    def _handle_record(self, content_type, version, payload):
        warnings = self._check_version(version)
        payload_len = len(payload)

        if payload_len == 0:
            self._empty_records += 1
            if self._empty_records > self.max_empty_records:
                self._fail("empty_record_flood")
            return self._event(
                "empty", content_type, version, payload, warnings,
                payload_len=payload_len,
            )

        if content_type == CT_CHANGE_CIPHER_SPEC:
            return self._handle_ccs(version, payload, warnings)

        if content_type == CT_HANDSHAKE:
            return self._handle_handshake(version, payload, warnings)

        if content_type == CT_APPLICATION_DATA:
            return self._handle_application_data(version, payload, warnings)

        if content_type == CT_ALERT:
            return self._handle_alert(version, payload, warnings)

        if content_type == CT_HEARTBEAT:
            return self._event(
                "heartbeat", content_type, version, payload, warnings,
                payload_len=payload_len,
            )

        self._errors["unknown_content_type"] += 1
        return self._event(
            "unknown", content_type, version, payload, warnings,
            payload_len=payload_len,
        )

    def _handle_ccs(self, version, payload, warnings):
        if self.negotiated_version == 0x0304:
            self._fail("unexpected_change_cipher_spec")
        if self._state == "closed":
            self._fail("connection_closed")

        self._ccs_records += 1
        if self._ccs_records > self.max_ccs_records:
            self._fail("ccs_flood")

        event = self._event(
            "change_cipher_spec", CT_CHANGE_CIPHER_SPEC, version, payload,
            warnings, payload_len=len(payload),
        )
        event["ccs_type"] = payload[0] if payload else None
        return event

    def _handle_handshake(self, version, payload, warnings):
        if self._state == "closed":
            self._fail("connection_closed")

        self._handshake_buf.extend(payload)
        messages = []

        while len(self._handshake_buf) >= 4:
            msg_type = self._handshake_buf[0]
            msg_len = int.from_bytes(self._handshake_buf[1:4], "big")
            if len(self._handshake_buf) < 4 + msg_len:
                break

            body = bytes(self._handshake_buf[4:4 + msg_len])
            del self._handshake_buf[:4 + msg_len]
            self._validate_handshake_message(msg_type)
            self._handshake_messages += 1
            messages.append({
                "type": msg_type,
                "length": msg_len,
                "body": body,
            })
            if msg_type == HT_FINISHED and self._state == "handshake":
                self._state = "established"

        event = self._event(
            "handshake", CT_HANDSHAKE, version, payload, warnings,
            payload_len=len(payload),
        )
        event["handshake_messages"] = messages

        if messages:
            first = messages[0]
            event["handshake_type"] = first["type"]
            event["handshake_length"] = first["length"]
            event["handshake_body"] = first["body"]
            event["handshake_complete"] = True
        else:
            event["handshake_complete"] = False
            if len(self._handshake_buf) >= 4:
                event["handshake_type"] = self._handshake_buf[0]
                event["handshake_length"] = int.from_bytes(
                    self._handshake_buf[1:4], "big"
                )
                event["handshake_body"] = bytes(self._handshake_buf[4:])
            else:
                event["handshake_type"] = None
                event["handshake_length"] = None
                event["handshake_body"] = b""
        return event

    def _handle_application_data(self, version, payload, warnings):
        if self._state == "closed":
            self._fail("application_data_after_close")
        if self._state == "handshake":
            if self._handshake_messages == 0:
                self._fail("application_data_before_handshake")
            self._fail("handshake_incomplete")

        return self._event(
            "application_data", CT_APPLICATION_DATA, version, payload,
            warnings, payload_len=len(payload),
        )

    def _handle_alert(self, version, payload, warnings):
        if self._state == "closed":
            self._fail("connection_closed")

        level = payload[0] if len(payload) > 0 else None
        description = payload[1] if len(payload) > 1 else None
        close_notify = description == 0
        event = self._event(
            "alert", CT_ALERT, version, payload, warnings,
            payload_len=len(payload),
        )
        event["alert_level"] = level
        event["alert_description"] = description
        event["close_notify"] = close_notify
        if close_notify:
            self._state = "closed"
        return event

    def _validate_handshake_message(self, msg_type):
        if self._state == "established":
            return

        if msg_type not in (HT_CLIENT_HELLO, HT_SERVER_HELLO,
                            HT_FINISHED, 0, 4, 11, 12, 13, 14, 15, 16,
                            24, 25):
            self._fail("unexpected_handshake")

        if msg_type == HT_CLIENT_HELLO:
            if self._seen_client_hello:
                self._fail("unexpected_handshake")
            self._seen_client_hello = True
        elif msg_type == HT_SERVER_HELLO:
            if self._seen_server_hello:
                self._fail("unexpected_handshake")
            self._seen_server_hello = True
        elif msg_type == HT_FINISHED:
            if self._handshake_messages == 0:
                self._fail("unexpected_handshake")

        self._handshake_started = True

    def _check_version(self, version):
        warnings = []
        if not self.negotiated_version:
            return warnings

        expected = (0x0303 if self.negotiated_version >= 0x0304
                    else self.negotiated_version)
        if version != expected:
            warnings.append("record_version_mismatch")
            self._warnings += 1
            self._warning_details.append({
                "kind": "record_version_mismatch",
                "expected": expected,
                "actual": version,
            })
        return warnings

    @staticmethod
    def _event(kind, content_type, version, payload, warnings, payload_len):
        return {
            "kind": kind,
            "content_type": content_type,
            "version": version,
            "record_version": version,
            "payload": payload,
            "payload_len": payload_len,
            "warnings": warnings,
        }

    def _fail(self, kind, fatal=True, detail=""):
        self._errors[kind] = self._errors.get(kind, 0) + 1
        if fatal:
            self._state = "closed"
            self._buf.clear()
            self._pos = 0
        raise RecordLayerError(kind, fatal=fatal, detail=detail)

    def _auto_compact(self):
        if self._pos <= 0:
            return
        if self._pos == len(self._buf):
            self._buf.clear()
        else:
            del self._buf[:self._pos]
        self._pos = 0
