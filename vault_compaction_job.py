"""vault_compaction_job (initial skeleton)."""


class VaultCompactionJob:
    """Vault compaction job.

    create_vault makes pages of page_size slots.  place fills the first free slots,
    splitting into stacks of stack_limit and returning placed, overflow and slots;
    during a running compaction it queues the item as pending instead.
    start_compact(vault_id, pages, now) locks the selected tabs: a second start on
    any tab returns busy, and a second vault for the same account_id returns
    account_busy.  compact(vault_id, pages, now, fail) is the one-shot convenience
    form (start followed by finish).  finish_compact(task_id, fail=False)
    compacts selected non-readonly tabs, skips locked items, merges stacks up to stack_limit, removes
    empty selected tabs, places pending items, bumps the version by one and
    returns status finished, version_before, version_after, moved, overflow and
    pending_placed.  fail=True restores the pre snapshot exactly.  A finished or
    failed task returns already_finished and never applies twice.  expand appends
    pages without changing existing page indices.  snapshot during compaction
    returns the pre snapshot with compacting True and version = version_before;
    otherwise it returns live pages, index and compacting False.  capacity
    recounts used slots and writes index.used.  operations returns the recorded
    move list (each row has item_id, count, from and to) and replay returns the
    resulting pages and version.  Status strings are placed, overflow and
    pending for place; started, busy and account_busy
    for start; finished, failed and already_finished for finish.
    """

    def __init__(self, stack_limit=20):
        raise NotImplementedError()

    def create_vault(self, vault_id, pages=2, page_size=10, account_id=None, realm=None):
        raise NotImplementedError()

    def set_page_readonly(self, vault_id, page, readonly=True):
        raise NotImplementedError()

    def set_locked(self, vault_id, item_id, locked=True):
        raise NotImplementedError()

    def place(self, vault_id, item, page=None):
        raise NotImplementedError()

    def start_compact(self, vault_id, pages=None, now=0.0):
        raise NotImplementedError()

    def compact(self, vault_id, pages=None, now=0.0, fail=False):
        raise NotImplementedError()

    def finish_compact(self, task_id, fail=False):
        raise NotImplementedError()

    def expand(self, vault_id, new_pages, page_size=None):
        raise NotImplementedError()

    def snapshot(self, vault_id):
        raise NotImplementedError()

    def capacity(self, vault_id):
        raise NotImplementedError()

    def operations(self, task_id):
        raise NotImplementedError()

    def replay(self, task_id):
        raise NotImplementedError()
