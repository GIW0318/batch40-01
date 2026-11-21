"""Tamper-evident audit log with anchors and Merkle inclusion proofs."""

import hashlib
import json
import threading


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
        self._lock = threading.Lock()

    def append(self, record, expected_length=None, version=None):
        with self._lock:
            if expected_length is not None and expected_length != len(self.entries):
                raise ConflictError(
                    "expected length %r but chain has %d entries"
                    % (expected_length, len(self.entries))
                )
            entry_version = self.version if version is None else version
            seq = len(self.entries)
            prev = self.entries[-1]["hash"] if self.entries else "0" * 64
            digest = self.entry_hash(seq, record, prev, version=entry_version)
            entry = {
                "seq": seq,
                "record": record,
                "prev": prev,
                "hash": digest,
                "version": entry_version,
            }
            self.entries.append(entry)
            return dict(entry)

    def entry_hash(self, seq, record, prev, version=None):
        entry_version = self.version if version is None else version
        canonical = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        payload = "v%d|%d|%s|%s" % (entry_version, seq, prev, canonical)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def verify(self, entries=None):
        entries = self.entries if entries is None else entries
        ordered = sorted(entries, key=lambda entry: entry["seq"])
        issues = []
        prev = "0" * 64
        for expected_seq, entry in enumerate(ordered):
            if entry["seq"] != expected_seq:
                issues.append({
                    "code": "sequence_gap",
                    "seq": entry["seq"],
                    "expected": expected_seq,
                })
            if entry["prev"] != prev:
                issues.append({
                    "code": "broken_link",
                    "seq": entry["seq"],
                })
            version = entry.get("version")
            if version not in self.supported_versions:
                issues.append({
                    "code": "unsupported_version",
                    "seq": entry["seq"],
                    "version": version,
                })
            else:
                recomputed = self.entry_hash(
                    entry["seq"], entry["record"], entry["prev"], version=version
                )
                if recomputed != entry["hash"]:
                    issues.append({
                        "code": "hash_mismatch",
                        "seq": entry["seq"],
                    })
            prev = entry["hash"]
        return {"ok": not issues, "issues": issues}

    def anchor(self, day):
        if not self.entries:
            return {"day": day, "seq": -1, "hash": None, "length": 0}
        tail = self.entries[-1]
        return {
            "day": day,
            "seq": tail["seq"],
            "hash": tail["hash"],
            "length": len(self.entries),
        }

    def verify_anchor(self, anchor, entries=None):
        entries = self.entries if entries is None else entries
        if len(entries) < anchor["length"]:
            return False
        if anchor["seq"] < 0:
            return anchor["length"] == 0
        ordered = sorted(entries, key=lambda entry: entry["seq"])
        anchored = None
        for entry in ordered:
            if entry["seq"] == anchor["seq"]:
                anchored = entry
                break
        if anchored is None:
            return False
        if anchored["hash"] != anchor["hash"]:
            return False
        version = anchored.get("version")
        if version not in self.supported_versions:
            return False
        recomputed = self.entry_hash(
            anchored["seq"], anchored["record"], anchored["prev"], version=version
        )
        return recomputed == anchor["hash"]

    def proof(self, index, entries=None):
        entries = self.entries if entries is None else entries
        ordered = sorted(entries, key=lambda entry: entry["seq"])
        if index < 0 or index >= len(ordered):
            raise IndexError("proof index %r out of range" % (index,))
        level = [entry["hash"] for entry in ordered]
        path = []
        position = index
        while len(level) > 1:
            if position % 2 == 0:
                sibling = position + 1
                side = "right"
            else:
                sibling = position - 1
                side = "left"
            if sibling < len(level):
                path.append({"position": side, "hash": level[sibling]})
            else:
                path.append({"position": side, "hash": level[position]})
            level = self._merkle_level(level)
            position //= 2
        return {
            "index": index,
            "hash": ordered[index]["hash"],
            "path": path,
            "root": level[0],
        }

    def verify_proof(self, entry_hash, proof, root=None):
        expected_root = proof["root"] if root is None else root
        current = entry_hash
        for step in proof["path"]:
            if step["position"] == "left":
                current = self._combine(step["hash"], current)
            else:
                current = self._combine(current, step["hash"])
        return current == expected_root

    def snapshot(self):
        head = self.entries[-1]["hash"] if self.entries else None
        return {"length": len(self.entries), "head": head}

    @staticmethod
    def _combine(left, right):
        return hashlib.sha256(
            bytes.fromhex(left) + bytes.fromhex(right)
        ).hexdigest()

    @classmethod
    def _merkle_level(cls, hashes):
        level = []
        for index in range(0, len(hashes), 2):
            left = hashes[index]
            right = hashes[index + 1] if index + 1 < len(hashes) else left
            level.append(cls._combine(left, right))
        return level
