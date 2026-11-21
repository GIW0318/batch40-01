"""Resumable plan for rotating data-encryption keys."""


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

    def __init__(self, old_version, new_version, deactivation_at=None,
                 grace_seconds=86400):
        self.old_version = str(old_version)
        self.new_version = str(new_version)
        self.deactivation_at = deactivation_at
        self.grace_seconds = grace_seconds

    def add(self, object_id, tenant, kind="object", dek_version=None):
        raise NotImplementedError

    def acquire(self):
        raise NotImplementedError

    def release(self):
        raise NotImplementedError

    def run(self, fail_ids=(), stop_after=None, idempotency_key=None):
        raise NotImplementedError

    def resume(self, fail_ids=()):
        raise NotImplementedError

    def status(self):
        raise NotImplementedError

    def audit(self):
        raise NotImplementedError

    def disable(self, version=None):
        raise NotImplementedError

    def wrapped_dek(self, tenant, object_id):
        raise NotImplementedError

    def unwrap_dek(self, tenant, token):
        raise NotImplementedError
