"""Multi-device cloud save conflict resolution.

Contract used by the tests
--------------------------
Canonical checksum: sha256 of
``json.dumps({"data": data, "deleted": deleted,
"schema_version": schema_version}, sort_keys=True,
separators=(",", ":"))`` encoded utf-8, as a hex digest.  When
``expected_checksum`` is supplied it must equal this value.

* ``begin_session(account, character_id, session_id, slot="default")``
  returns {"role": "writer"} for the first active session of a slot and
  {"role": "readonly"} for additional sessions.  Re-beginning the same
  session returns its existing role.  ``end_session`` releases the writer.
* ``upload(account, character_id, data, version_vector, updated_at=0,
  checksum=None, expected_checksum=None, complete=True, session_id=None,
  slot="default", deleted=False, schema_version=None)``:
  - complete=False -> {"status": "pending"}; the active save is unchanged.
  - expected_checksum mismatch -> {"status": "corrupt"}; active unchanged.
  - a readonly session stores a conflict draft and returns {"status":
    "draft", "conflict_id": ...}; active unchanged.
  - a version vector that dominates the active one stores the save;
  - equal vectors: a strictly larger updated_at stores the save (server time
    fallback), otherwise {"status": "stale"};
  - neither vector dominates -> {"status": "conflict", "conflict_id": ...};
    a draft is stored and the active save is unchanged;
  - a tombstone (deleted=True) that dominates the active save is stored
    with status "deleted"; later non-deleted uploads only win when their
    vector strictly dominates the tombstone, otherwise "stale".
  Every result contains status, character_id and slot.
* ``download(account, character_id, slot="default", session_id=None)``
  returns None when there is no save, else a dict with character_id, slot,
  schema_version, version_vector, updated_at, deleted, data, role.
* ``merge(base, left, right)`` returns a new dict: dicts merge recursively;
  numbers take max; lists are sorted unions; strings prefer the non-empty
  right side; missing values copy from the other side; fields present in
  only one version are preserved.
* ``resolve(account, character_id, slot, conflict_id)`` merges the draft
  into the active save, merges version vectors with per-key max, keeps the
  larger updated_at, stores the result and returns {"status": "resolved",
  "character_id", "slot", "schema_version", "data"}.  Unknown conflict ids
  raise KeyError.
* Saves are isolated by (account, character_id, slot).
"""


class CloudsaveConflictMerge:
    def __init__(self, schema_version=2):
        self.schema_version = schema_version

    def begin_session(self, account, character_id, session_id,
                      slot="default"):
        raise NotImplementedError

    def end_session(self, account, character_id, slot="default"):
        raise NotImplementedError

    def upload(self, account, character_id, data, version_vector,
               updated_at=0, checksum=None, expected_checksum=None,
               complete=True, session_id=None, slot="default",
               deleted=False, schema_version=None):
        raise NotImplementedError

    def download(self, account, character_id, slot="default",
                 session_id=None):
        raise NotImplementedError

    def merge(self, base, left, right):
        raise NotImplementedError

    def resolve(self, account, character_id, slot, conflict_id):
        raise NotImplementedError
