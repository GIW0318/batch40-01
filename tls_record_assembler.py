"""Incremental TLS record assembler used by the auditing proxy."""


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

    def __init__(self, negotiated_version=0x0303, max_record_len=16384,
                 max_empty_records=4, max_ccs_records=8, max_buffer_bytes=65536):
        raise NotImplementedError()

    def feed(self, data):
        raise NotImplementedError()

    def pending(self):
        raise NotImplementedError()

    def compact(self):
        raise NotImplementedError()

    def on_decrypt_failure(self, reason):
        raise NotImplementedError()

    def errors(self):
        raise NotImplementedError()

    def audit(self):
        raise NotImplementedError()
