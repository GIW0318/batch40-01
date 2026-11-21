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

import copy
import hashlib
import json
import threading
import uuid
from dataclasses import dataclass


_MISSING = object()


@dataclass
class _Save:
    data: object
    deleted: bool
    schema_version: int
    version_vector: dict
    updated_at: int
    checksum: str


@dataclass
class _Conflict:
    account: str
    character_id: str
    slot: str
    draft: _Save
    base: _Save | None


class CloudsaveConflictMerge:
    def __init__(self, schema_version=2):
        self.schema_version = schema_version
        self._saves = {}
        self._staging = {}
        self._conflicts = {}
        self._sessions = {}
        self._writers = {}
        self._upload_lock = threading.RLock()

    @staticmethod
    def _save_key(account, character_id, slot):
        return (account, character_id, slot)

    @staticmethod
    def _canonical_checksum(data, deleted, schema_version):
        payload = {
            "data": data,
            "deleted": bool(deleted),
            "schema_version": int(schema_version),
        }
        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _normalize_vector(version_vector):
        if not version_vector:
            return {}
        return {str(key): int(value) for key, value in version_vector.items()}

    @staticmethod
    def _equal_vectors(left, right):
        return all(
            left.get(key, 0) == right.get(key, 0)
            for key in set(left) | set(right)
        )

    @staticmethod
    def _dominates(left, right):
        keys = set(left) | set(right)
        return (
            all(left.get(key, 0) >= right.get(key, 0) for key in keys)
            and any(left.get(key, 0) > right.get(key, 0) for key in keys)
        )

    @staticmethod
    def _merge_vectors(*vectors):
        merged = {}
        for vector in vectors:
            for key, value in vector.items():
                merged[key] = max(merged.get(key, 0), value)
        return merged

    def _new_save(self, data, deleted, schema_version, version_vector,
                  updated_at):
        normalized_schema = int(schema_version)
        normalized_data = None if deleted else copy.deepcopy(data)
        return _Save(
            data=normalized_data,
            deleted=bool(deleted),
            schema_version=normalized_schema,
            version_vector=self._normalize_vector(version_vector),
            updated_at=int(updated_at or 0),
            checksum=self._canonical_checksum(
                normalized_data, deleted, normalized_schema
            ),
        )

    def _clone_save(self, save):
        return _Save(
            data=copy.deepcopy(save.data),
            deleted=save.deleted,
            schema_version=save.schema_version,
            version_vector=dict(save.version_vector),
            updated_at=save.updated_at,
            checksum=save.checksum,
        )

    def _migrate_data(self, data, from_version, to_version):
        """Migrate known legacy shapes without discarding unknown fields.

        The exercise intentionally does not prescribe field renames.  The
        important cross-version property is that migration preserves fields
        unknown to the old reader and that a later merge cannot erase them.
        """
        del from_version, to_version
        return copy.deepcopy(data)

    def _migrate_save(self, save, target_schema):
        migrated = self._clone_save(save)
        if migrated.schema_version < target_schema:
            migrated.data = self._migrate_data(
                migrated.data,
                migrated.schema_version,
                target_schema,
            )
            migrated.schema_version = target_schema
        migrated.checksum = self._canonical_checksum(
            migrated.data, migrated.deleted, migrated.schema_version
        )
        return migrated

    def _session_role(self, key, session_id):
        if session_id is None:
            return "readonly" if key in self._writers else "writer"

        sessions = self._sessions.setdefault(key, {})
        role = sessions.get(session_id)
        if role is not None:
            return role

        writer = self._writers.get(key)
        if writer is None:
            role = "writer"
            self._writers[key] = session_id
        else:
            role = "readonly"
        sessions[session_id] = role
        return role

    def _result(self, status, character_id, slot, save=None, **extra):
        result = {
            "status": status,
            "character_id": character_id,
            "slot": slot,
        }
        if save is not None:
            result.update({
                "schema_version": save.schema_version,
                "version_vector": dict(save.version_vector),
                "updated_at": save.updated_at,
                "checksum": save.checksum,
            })
        result.update(extra)
        return result

    def _store_conflict(self, account, character_id, slot, draft, base):
        conflict_id = uuid.uuid4().hex
        self._conflicts[conflict_id] = _Conflict(
            account=account,
            character_id=character_id,
            slot=slot,
            draft=self._clone_save(draft),
            base=self._clone_save(base) if base is not None else None,
        )
        return conflict_id

    @staticmethod
    def _canonical_item(item):
        try:
            return json.dumps(
                item, sort_keys=True, separators=(",", ":"), default=repr
            )
        except (TypeError, ValueError):
            return repr(item)

    @classmethod
    def _merge_lists(cls, values):
        merged = {}
        for value in values:
            for item in value:
                key = cls._canonical_item(item)
                merged[key] = item

        def sort_key(item):
            original = merged[item]
            if isinstance(original, (int, float, str)):
                return (0, type(original).__name__, original)
            return (1, type(original).__name__, item)

        return [
            copy.deepcopy(merged[key])
            for key in sorted(merged, key=sort_key)
        ]

    @staticmethod
    def _status_rank(value):
        if not isinstance(value, str):
            return None
        normalized = value.strip().casefold().replace("-", "_").replace(
            " ", "_"
        )
        ranks = {
            "completed": 100,
            "complete": 100,
            "done": 100,
            "finished": 100,
            "turned_in": 95,
            "submitted": 90,
            "claimed": 90,
            "已提交": 90,
            "已完成": 100,
            "已交付": 95,
            "in_progress": 50,
            "started": 45,
            "active": 40,
            "available": 30,
            "not_started": 20,
            "pending": 20,
            "locked": 10,
        }
        return ranks.get(normalized)

    def _merge_statuses(self, values):
        non_empty = [value for value in values if value not in (None, "")]
        if not non_empty:
            return ""
        ranked = [
            (self._status_rank(value), index, value)
            for index, value in enumerate(non_empty)
            if self._status_rank(value) is not None
        ]
        if ranked:
            return max(ranked, key=lambda item: (item[0], item[1]))[2]
        return non_empty[-1]

    @classmethod
    def _is_status_path(cls, path):
        for part in path:
            normalized = str(part).casefold().replace("-", "_").replace(
                " ", "_"
            )
            if (
                normalized in {"status", "state", "statuses", "states"}
                or normalized.endswith("_status")
                or normalized.endswith("_state")
            ):
                return True
        return False

    def _merge_values(self, base, left, right, path=()):
        values = [
            value for value in (base, left, right) if value is not _MISSING
        ]
        if not values:
            return _MISSING

        present = [value for value in values if value is not None]
        if not present:
            return None

        dicts = [value for value in present if isinstance(value, dict)]
        if dicts and len(dicts) == len(present):
            result = {}
            keys = set()
            for value in values:
                if isinstance(value, dict):
                    keys.update(value.keys())
            for key in sorted(keys, key=str):
                item = self._merge_values(
                    base.get(key, _MISSING)
                    if isinstance(base, dict) else _MISSING,
                    left.get(key, _MISSING)
                    if isinstance(left, dict) else _MISSING,
                    right.get(key, _MISSING)
                    if isinstance(right, dict) else _MISSING,
                    path + (key,),
                )
                if item is not _MISSING:
                    result[key] = item
            return result

        lists = [value for value in present if isinstance(value, list)]
        if lists and len(lists) == len(present):
            return self._merge_lists(lists)

        key = path[-1] if path else None
        if key in {"status", "state", "quest_status", "task_status"}:
            statuses = [
                value for value in (left, right) if value is not _MISSING
            ]
            if not statuses:
                statuses = present
            return self._merge_statuses(statuses)

        numbers = [
            value
            for value in present
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if numbers and len(numbers) == len(present):
            return max(numbers)

        booleans = [value for value in present if isinstance(value, bool)]
        if booleans and len(booleans) == len(present):
            return any(booleans)

        strings = [value for value in present if isinstance(value, str)]
        if strings and len(strings) == len(present):
            non_empty_strings = [value for value in strings if value != ""]
            if non_empty_strings and (
                self._is_status_path(path)
                or all(
                    self._status_rank(value) is not None
                    for value in non_empty_strings
                )
            ):
                return self._merge_statuses(
                    [
                        value
                        for value in (left, right)
                        if value is not _MISSING
                    ]
                )
            for value in (right, left, base):
                if (
                    value is not _MISSING
                    and isinstance(value, str)
                    and value != ""
                ):
                    return value
            return ""

        if right is not _MISSING and right is not None:
            return copy.deepcopy(right)
        if left is not _MISSING and left is not None:
            return copy.deepcopy(left)
        if base is not _MISSING and base is not None:
            return copy.deepcopy(base)
        return copy.deepcopy(present[-1])

    def begin_session(self, account, character_id, session_id,
                      slot="default"):
        key = self._save_key(account, character_id, slot)
        with self._upload_lock:
            role = self._session_role(key, session_id)
            return {"role": role}

    def end_session(self, account, character_id, slot="default"):
        key = self._save_key(account, character_id, slot)
        with self._upload_lock:
            writer = self._writers.pop(key, None)
            if writer is not None:
                self._sessions.get(key, {}).pop(writer, None)

    def upload(self, account, character_id, data, version_vector,
               updated_at=0, checksum=None, expected_checksum=None,
               complete=True, session_id=None, slot="default",
               deleted=False, schema_version=None):
        key = self._save_key(account, character_id, slot)
        with self._upload_lock:
            incoming_schema = int(
                self.schema_version
                if schema_version is None else schema_version
            )
            incoming = self._new_save(
                data,
                deleted,
                incoming_schema,
                version_vector,
                updated_at,
            )

            actual_checksum = incoming.checksum
            if checksum is not None and checksum != actual_checksum:
                return self._result(
                    "corrupt", character_id, slot, incoming
                )
            if (
                expected_checksum is not None
                and expected_checksum != actual_checksum
            ):
                return self._result(
                    "corrupt", character_id, slot, incoming
                )

            if not complete:
                self._staging[key] = (session_id, incoming)
                return self._result(
                    "pending",
                    character_id,
                    slot,
                    incoming,
                    upload_id=uuid.uuid4().hex,
                )

            role = self._session_role(key, session_id)
            active = self._saves.get(key)

            if role == "readonly":
                draft = self._migrate_save(
                    incoming,
                    max(
                        self.schema_version,
                        incoming.schema_version,
                        active.schema_version if active else 0,
                    ),
                )
                conflict_id = self._store_conflict(
                    account, character_id, slot, draft, active
                )
                return self._result(
                    "draft",
                    character_id,
                    slot,
                    draft,
                    conflict_id=conflict_id,
                )

            if active is None:
                self._staging.pop(key, None)
                self._saves[key] = incoming
                return self._result(
                    "deleted" if incoming.deleted else "stored",
                    character_id,
                    slot,
                    incoming,
                )

            active_vector = active.version_vector
            incoming_vector = incoming.version_vector
            equal_vectors = self._equal_vectors(
                incoming_vector, active_vector
            )
            incoming_dominates = self._dominates(
                incoming_vector, active_vector
            )
            active_dominates = self._dominates(
                active_vector, incoming_vector
            )

            if active.deleted and not incoming.deleted:
                if self._dominates(incoming_vector, active_vector):
                    decision = "store"
                else:
                    decision = "stale"
            elif incoming.deleted:
                if incoming_dominates:
                    decision = "store"
                elif equal_vectors:
                    decision = (
                        "store"
                        if incoming.updated_at > active.updated_at
                        else "stale"
                    )
                elif active_dominates:
                    decision = "stale"
                else:
                    decision = "conflict"
            elif incoming_dominates:
                decision = "store"
            elif equal_vectors:
                decision = (
                    "store"
                    if incoming.updated_at > active.updated_at
                    else "stale"
                )
            elif active_dominates:
                decision = "stale"
            else:
                decision = "conflict"

            if decision == "conflict":
                target_schema = max(
                    self.schema_version,
                    active.schema_version,
                    incoming.schema_version,
                )
                draft = self._migrate_save(incoming, target_schema)
                conflict_id = self._store_conflict(
                    account, character_id, slot, draft, active
                )
                return self._result(
                    "conflict",
                    character_id,
                    slot,
                    draft,
                    conflict_id=conflict_id,
                )

            if decision == "stale":
                return self._result("stale", character_id, slot, incoming)

            target_schema = max(
                self.schema_version,
                active.schema_version,
                incoming.schema_version,
            )
            migrated_active = self._migrate_save(active, target_schema)
            migrated_incoming = self._migrate_save(
                incoming, target_schema
            )

            if (
                active.schema_version != target_schema
                or incoming.schema_version != target_schema
                or active.schema_version != incoming.schema_version
            ):
                if migrated_incoming.deleted:
                    migrated_incoming.data = None
                else:
                    migrated_incoming.data = self._merge_values(
                        migrated_active.data,
                        migrated_active.data,
                        migrated_incoming.data,
                    )
                migrated_incoming.checksum = self._canonical_checksum(
                    migrated_incoming.data,
                    migrated_incoming.deleted,
                    migrated_incoming.schema_version,
                )

            self._staging.pop(key, None)
            self._saves[key] = migrated_incoming
            return self._result(
                "deleted" if migrated_incoming.deleted else "stored",
                character_id,
                slot,
                migrated_incoming,
            )

    def download(self, account, character_id, slot="default",
                 session_id=None):
        key = self._save_key(account, character_id, slot)
        with self._upload_lock:
            active = self._saves.get(key)
            if active is None:
                return None
            role = self._session_role(key, session_id)
            return {
                "character_id": character_id,
                "slot": slot,
                "schema_version": active.schema_version,
                "version_vector": dict(active.version_vector),
                "updated_at": active.updated_at,
                "deleted": active.deleted,
                "data": copy.deepcopy(active.data),
                "role": role,
                "checksum": active.checksum,
            }

    def merge(self, base, left, right):
        with self._upload_lock:
            merged = self._merge_values(base, left, right)
            return {} if merged is _MISSING else merged

    def resolve(self, account, character_id, slot, conflict_id):
        key = self._save_key(account, character_id, slot)
        with self._upload_lock:
            conflict = self._conflicts.get(conflict_id)
            if (
                conflict is None
                or conflict.account != account
                or conflict.character_id != character_id
                or conflict.slot != slot
            ):
                raise KeyError(conflict_id)

            active = self._saves.get(key)
            target_schema = max(
                self.schema_version,
                conflict.draft.schema_version,
                active.schema_version if active else 0,
            )
            draft = self._migrate_save(conflict.draft, target_schema)
            migrated_active = (
                self._migrate_save(active, target_schema)
                if active is not None else None
            )

            base_save = conflict.base
            if base_save is not None:
                base_save = self._migrate_save(base_save, target_schema)
            base_data = base_save.data if base_save is not None else _MISSING
            active_data = (
                migrated_active.data
                if migrated_active is not None else _MISSING
            )
            merged_data = self._merge_values(
                base_data, active_data, draft.data
            )
            if merged_data is _MISSING:
                merged_data = {}

            if migrated_active is not None:
                vectors = self._merge_vectors(
                    migrated_active.version_vector,
                    draft.version_vector,
                )
                updated_at = max(
                    migrated_active.updated_at, draft.updated_at
                )
                deleted = migrated_active.deleted or draft.deleted
            else:
                vectors = dict(draft.version_vector)
                updated_at = draft.updated_at
                deleted = draft.deleted

            if deleted:
                merged_data = None
            resolved_save = self._new_save(
                merged_data,
                deleted,
                target_schema,
                vectors,
                updated_at,
            )
            self._saves[key] = resolved_save
            del self._conflicts[conflict_id]
            return {
                "status": "resolved",
                "character_id": character_id,
                "slot": slot,
                "schema_version": resolved_save.schema_version,
                "version_vector": dict(resolved_save.version_vector),
                "updated_at": resolved_save.updated_at,
                "data": copy.deepcopy(resolved_save.data),
                "deleted": resolved_save.deleted,
                "checksum": resolved_save.checksum,
            }
