"""Tamper-evident audit log with anchors and Merkle inclusion proofs."""


class ConflictError(RuntimeError):
    """Raised when a concurrent writer appends from a stale chain tail."""


class AuditHashChain:
    """Append-only hash chain over audit records.

    ``version`` is the serialization version used for new entries,
    ``supported_versions`` lists every version the verifier can recompute and
    ``salt`` is private (never included in snapshots).  Live entries are kept
    in ``self.entries``.

    ``append(record, expected_length=None, version=None)`` appends a record at
    the next sequence number and returns
    ``{"seq", "record", "prev", "hash", "version"}``.  The first ``prev`` is
    64 zeroes.  When ``expected_length`` is given and differs from the current
    length a ``ConflictError`` is raised before anything is appended.

    ``entry_hash(seq, record, prev, version=None)`` computes the entry hash
    without mutating the chain, so old entries can be rebuilt and checked.  It
    is the sha256 hex digest of ``"v<version>|<seq>|<prev>|"`` followed by the
    compact, key-sorted JSON rendering of the record.

    ``verify(entries=None)`` returns ``{"ok", "issues"}``.  Entries are sorted
    by ``seq`` (wall-clock order is irrelevant) and must have contiguous
    sequence numbers starting at 0 (``sequence_gap``), a matching ``prev``
    link (``broken_link``), a recomputable hash (``hash_mismatch``) and a
    version in ``supported_versions`` (``unsupported_version``).  Unsupported
    versions are reported, never skipped.

    ``anchor(day)`` returns ``{"day", "seq", "hash", "length"}`` describing
    the current tail.  ``verify_anchor(anchor, entries=None)`` recomputes the
    anchored entry's hash and returns False when the chain is shorter than the
    anchor (tail deletion) or the anchored hash no longer matches.

    ``proof(index, entries=None)`` returns a Merkle proof
    ``{"index", "hash", "path", "root"}`` over the batch of entry hashes.
    ``verify_proof(entry_hash, proof, root=None)`` folds the path and compares
    against ``root`` (or ``proof["root"]``); a proof never verifies against a
    different batch root.

    ``snapshot()`` returns only ``{"length", "head"}``.
    """

    def __init__(self, version=2, supported_versions=(1, 2), salt=None):
        self.version = version
        self.supported_versions = tuple(supported_versions)
        self.salt = salt
        self.entries = []

    def append(self, record, expected_length=None, version=None):
        raise NotImplementedError

    def entry_hash(self, seq, record, prev, version=None):
        raise NotImplementedError

    def verify(self, entries=None):
        raise NotImplementedError

    def anchor(self, day):
        raise NotImplementedError

    def verify_anchor(self, anchor, entries=None):
        raise NotImplementedError

    def proof(self, index, entries=None):
        raise NotImplementedError

    def verify_proof(self, entry_hash, proof, root=None):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
