"""Resumable plan for rotating data-encryption keys."""

import threading
import uuid
from datetime import datetime, timedelta, timezone


class DekRotationPlan:
    """Rotate DEKs from an old master key to a new one exactly once.

    ``old_version`` and ``new_version`` are exact version strings: ``"1"``
    never matches ``"12"``.  ``deactivation_at`` is recorded for the old
    version; when omitted it is derived from ``grace_seconds``.

    ``add(object_id, tenant, kind="object", dek_version=None)`` registers an
    object.  ``kind`` is ``"object"``, ``"backup"`` or ``"index"``.  A
    ``dek_version`` other than ``old_version`` raises ``ValueError`` (this
    includes the ``"12"`` / ``"1"`` prefix case).

    ``acquire()`` takes the per-plan rotation lock and raises ``RuntimeError``
    when a rotation is already running; ``release()`` frees it.

    ``run(fail_ids=(), stop_after=None, idempotency_key=None)`` rewraps
    pending objects in registration order, marks ``fail_ids`` as failed, and
    stops after ``stop_after`` newly rewrapped objects.  It returns
    ``status()``.  Running a completed plan again is a no-op.  A repeated
    ``idempotency_key`` returns the cached status without touching objects.

    ``resume(fail_ids=())`` retries every failed or pending object and never
    rewraps an object twice.

    ``status()`` returns ``{"total", "rotated", "pending", "failed",
    "complete", "old_version", "new_version", "deactivation_at"}`` where
    ``pending = total - rotated - failed`` and ``complete`` is True only when
    ``rotated == total``.

    ``audit()`` returns one ``{"object_id", "tenant", "kind", "from", "to"}``
    record per successful rewrap, in rotation order.

    ``disable(version=None)`` refuses with ``RuntimeError`` while any object
    is pending or failed, raises ``ValueError`` for a version that is not the
    exact old version, and otherwise disables the old version and returns
    True.

    ``wrapped_dek(tenant, object_id)`` returns an opaque token bound to the
    tenant.  ``unwrap_dek(tenant, token)`` raises ``PermissionError`` when the
    token belongs to another tenant and otherwise returns the object id.
    """

    _KINDS = ("object", "backup", "index")
    _PENDING = "pending"
    _ROTATED = "rotated"
    _FAILED = "failed"

    def __init__(self, old_version, new_version, deactivation_at=None,
                 grace_seconds=86400):
        self.old_version = str(old_version)
        self.new_version = str(new_version)
        self.grace_seconds = grace_seconds
        if deactivation_at is None:
            deadline = datetime.now(timezone.utc) + timedelta(
                seconds=grace_seconds)
            deactivation_at = deadline.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.deactivation_at = deactivation_at
        self._objects = []
        self._audit = []
        self._idempotency = {}
        self._tokens = {}
        self._rotation_lock = threading.Lock()
        self._disabled = False

    def add(self, object_id, tenant, kind="object", dek_version=None):
        if kind not in self._KINDS:
            raise ValueError("unknown object kind: %r" % (kind,))
        if dek_version is None:
            dek_version = self.old_version
        if str(dek_version) != self.old_version:
            raise ValueError(
                "dek_version %r does not exactly match old version %r"
                % (dek_version, self.old_version))
        self._objects.append({
            "object_id": object_id,
            "tenant": tenant,
            "kind": kind,
            "state": self._PENDING,
        })

    def acquire(self):
        if not self._rotation_lock.acquire(blocking=False):
            raise RuntimeError("a rotation is already running for this key")

    def release(self):
        if self._rotation_lock.locked():
            self._rotation_lock.release()

    def _rewrap(self, record):
        record["state"] = self._ROTATED
        self._audit.append({
            "object_id": record["object_id"],
            "tenant": record["tenant"],
            "kind": record["kind"],
            "from": self.old_version,
            "to": self.new_version,
        })

    def run(self, fail_ids=(), stop_after=None, idempotency_key=None):
        if (idempotency_key is not None
                and idempotency_key in self._idempotency):
            return dict(self._idempotency[idempotency_key])
        fail_ids = set(fail_ids)
        rewrapped = 0
        for record in self._objects:
            if record["state"] != self._PENDING:
                continue
            if record["object_id"] in fail_ids:
                record["state"] = self._FAILED
                continue
            self._rewrap(record)
            rewrapped += 1
            if stop_after is not None and rewrapped >= stop_after:
                break
        status = self.status()
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = dict(status)
        return status

    def resume(self, fail_ids=()):
        fail_ids = set(fail_ids)
        for record in self._objects:
            if record["state"] not in (self._PENDING, self._FAILED):
                continue
            if record["object_id"] in fail_ids:
                record["state"] = self._FAILED
            else:
                self._rewrap(record)
        return self.status()

    def status(self):
        total = len(self._objects)
        rotated = sum(
            1 for record in self._objects
            if record["state"] == self._ROTATED)
        failed = sum(
            1 for record in self._objects
            if record["state"] == self._FAILED)
        return {
            "total": total,
            "rotated": rotated,
            "pending": total - rotated - failed,
            "failed": failed,
            "complete": rotated == total,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "deactivation_at": self.deactivation_at,
        }

    def audit(self):
        return [dict(record) for record in self._audit]

    def disable(self, version=None):
        if any(record["state"] != self._ROTATED
               for record in self._objects):
            raise RuntimeError(
                "cannot disable the old version while objects are pending "
                "or failed")
        if version is not None and str(version) != self.old_version:
            raise ValueError(
                "version %r is not the exact old version %r"
                % (version, self.old_version))
        self._disabled = True
        return True

    def wrapped_dek(self, tenant, object_id):
        token = uuid.uuid4().hex
        self._tokens[token] = (tenant, object_id)
        return token

    def unwrap_dek(self, tenant, token):
        if token not in self._tokens:
            raise ValueError("unknown wrapped dek token")
        owner_tenant, object_id = self._tokens[token]
        if owner_tenant != tenant:
            raise PermissionError(
                "token belongs to tenant %r, not %r"
                % (owner_tenant, tenant))
        return object_id
