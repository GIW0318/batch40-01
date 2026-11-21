"""Vault compaction job."""

from copy import deepcopy


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
        if int(stack_limit) <= 0:
            raise ValueError("stack_limit must be positive")
        self.stack_limit = int(stack_limit)
        self._vaults = {}
        self._tasks = {}
        self._tab_locks = {}
        self._account_locks = {}
        self._next_task_id = 1

    def create_vault(self, vault_id, pages=2, page_size=10, account_id=None, realm=None):
        if int(pages) < 1:
            raise ValueError("pages must be at least 1")
        if int(page_size) < 1:
            raise ValueError("page_size must be at least 1")
        page_size = int(page_size)
        page_list = [[None] * page_size for _ in range(int(pages))]
        capacity = page_size * int(pages)
        self._vaults[vault_id] = {
            "pages": page_list,
            "page_sizes": [page_size] * int(pages),
            "page_size": page_size,
            "readonly": set(),
            "locked": set(),
            "account_id": account_id,
            "realm": realm,
            "version": 0,
            "index": {"used": 0, "capacity": capacity, "free": capacity},
            "active_task": None,
        }
        return self._live_snapshot(vault_id)

    def set_page_readonly(self, vault_id, page, readonly=True):
        vault = self._vault(vault_id)
        page = int(page)
        if page < 0 or page >= len(vault["pages"]):
            raise IndexError("page out of range")
        if readonly:
            vault["readonly"].add(page)
        else:
            vault["readonly"].discard(page)
        return self._live_snapshot(vault_id)

    def set_locked(self, vault_id, item_id, locked=True):
        vault = self._vault(vault_id)
        if locked:
            vault["locked"].add(item_id)
        else:
            vault["locked"].discard(item_id)
        return self._live_snapshot(vault_id)

    def place(self, vault_id, item, page=None):
        vault = self._vault(vault_id)
        task_id = vault["active_task"]
        if task_id is not None:
            task = self._tasks[task_id]
            task["pending"].append(
                {"item": deepcopy(item), "page": None if page is None else int(page)}
            )
            return {
                "status": "pending",
                "task_id": task_id,
                "placed": 0,
                "overflow": 0,
                "slots": [],
            }

        result = self._insert_item(
            vault["pages"],
            vault["page_sizes"],
            vault["readonly"],
            item,
            page=None if page is None else int(page),
        )
        self._recount(vault)
        result["status"] = "overflow" if result["overflow"] else "placed"
        return result

    def start_compact(self, vault_id, pages=None, now=0.0):
        vault = self._vault(vault_id)
        if vault["active_task"] is not None:
            return {
                "status": "busy",
                "task_id": vault["active_task"],
                "version_before": vault["version"],
            }

        selected = self._selected_pages(vault, pages)
        for page in selected:
            locked_by = self._tab_locks.get((vault_id, page))
            if locked_by is not None:
                return {
                    "status": "busy",
                    "task_id": locked_by,
                    "version_before": vault["version"],
                }

        account_id = vault["account_id"]
        if account_id is not None:
            for other_task_id, other_realm in self._account_locks.get(account_id, {}).items():
                other_task = self._tasks.get(other_task_id)
                if (
                    other_task is not None
                    and other_task["status"] == "running"
                    and other_realm != vault["realm"]
                ):
                    return {
                        "status": "account_busy",
                        "task_id": other_task_id,
                        "version_before": vault["version"],
                    }

        task_id = self._next_task_id
        self._next_task_id += 1
        pre_state = {
            "pages": deepcopy(vault["pages"]),
            "page_sizes": list(vault["page_sizes"]),
            "readonly": set(vault["readonly"]),
            "locked": set(vault["locked"]),
            "index": deepcopy(vault["index"]),
            "version": vault["version"],
        }
        self._tasks[task_id] = {
            "task_id": task_id,
            "vault_id": vault_id,
            "pages": selected,
            "status": "running",
            "version_before": vault["version"],
            "version_after": None,
            "pre_state": pre_state,
            "pending": [],
            "operations": [],
            "after_pages": None,
            "moved": 0,
            "overflow": 0,
            "pending_placed": 0,
            "now": now,
        }
        vault["active_task"] = task_id
        for page in selected:
            self._tab_locks[(vault_id, page)] = task_id
        if account_id is not None:
            self._account_locks.setdefault(account_id, {})[task_id] = vault["realm"]

        return {
            "status": "started",
            "task_id": task_id,
            "version_before": vault["version"],
        }

    def compact(self, vault_id, pages=None, now=0.0, fail=False):
        started = self.start_compact(vault_id, pages=pages, now=now)
        if started["status"] != "started":
            return started
        return self.finish_compact(started["task_id"], fail=fail)

    def finish_compact(self, task_id, fail=False):
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task["status"] in ("finished", "failed"):
            return {
                "status": "already_finished",
                "task_id": task_id,
                "version_before": task["version_before"],
                "version_after": task["version_after"],
                "moved": task["moved"],
                "overflow": task["overflow"],
                "pending_placed": task["pending_placed"],
            }

        vault = self._vault(task["vault_id"])
        try:
            if fail:
                return self._finish_failed(task, vault)
            return self._finish_success(task, vault)
        finally:
            vault["active_task"] = None
            for page in task["pages"]:
                if self._tab_locks.get((task["vault_id"], page)) == task_id:
                    del self._tab_locks[(task["vault_id"], page)]
            account_id = vault["account_id"]
            if account_id is not None:
                locks = self._account_locks.get(account_id)
                if locks is not None:
                    locks.pop(task_id, None)
                    if not locks:
                        del self._account_locks[account_id]

    def expand(self, vault_id, new_pages, page_size=None):
        vault = self._vault(vault_id)
        new_pages = int(new_pages)
        if new_pages < 0:
            raise ValueError("new_pages must not be negative")
        size = vault["page_size"] if page_size is None else int(page_size)
        if size < 1:
            raise ValueError("page_size must be positive")

        added = [[None] * size for _ in range(new_pages)]
        new_pages_list = vault["pages"] + added
        new_sizes = vault["page_sizes"] + [size] * new_pages
        vault["pages"] = new_pages_list
        vault["page_sizes"] = new_sizes
        self._recount(vault)
        return self._live_snapshot(vault_id)

    def snapshot(self, vault_id):
        vault = self._vault(vault_id)
        task_id = vault["active_task"]
        if task_id is not None:
            pre = self._tasks[task_id]["pre_state"]
            return {
                "pages": deepcopy(pre["pages"]),
                "index": deepcopy(pre["index"]),
                "version": pre["version"],
                "compacting": True,
            }
        return self._live_snapshot(vault_id)

    def capacity(self, vault_id):
        vault = self._vault(vault_id)
        self._recount(vault)
        return deepcopy(vault["index"])

    def operations(self, task_id):
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return deepcopy(task["operations"])

    def replay(self, task_id):
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task["after_pages"] is None:
            pre = task["pre_state"]
            return {"pages": deepcopy(pre["pages"]), "version": pre["version"]}
        return {
            "pages": deepcopy(task["after_pages"]),
            "version": task["version_after"],
        }

    def _finish_success(self, task, vault):
        working_pages = deepcopy(vault["pages"])
        working_sizes = list(vault["page_sizes"])
        working_readonly = set(vault["readonly"])
        working_locked = set(vault["locked"])

        pages, sizes, readonly, operations, overflow = self._compact_layout(
            working_pages,
            working_sizes,
            working_readonly,
            working_locked,
            task["pages"],
        )

        pending_placed = 0
        pending_operations = []
        for entry in task["pending"]:
            placed = self._insert_item(
                pages,
                sizes,
                readonly,
                entry["item"],
                page=None,
                auto_expand=True,
                new_page_size=vault["page_size"],
            )
            overflow += placed["overflow"]
            if placed["placed"] > 0:
                pending_placed += 1
                for chunk in placed["chunks"]:
                    pending_operations.append(
                        {
                            "item_id": chunk["item_id"],
                            "count": chunk["count"],
                            "from": None,
                            "to": {"page": chunk["page"], "slot": chunk["slot"]},
                        }
                    )

        revision_before = task["version_before"]
        revision_after = revision_before + 1
        vault["pages"] = pages
        vault["page_sizes"] = sizes
        vault["readonly"] = {page for page in readonly if page < len(pages)}
        vault["version"] = revision_after
        self._recount(vault)

        task["operations"] = operations + pending_operations
        task["after_pages"] = deepcopy(vault["pages"])
        task["version_after"] = revision_after
        task["moved"] = sum(
            operation["count"]
            for operation in operations
            if operation["from"] is not None
        )
        task["overflow"] = overflow
        task["pending_placed"] = pending_placed
        task["status"] = "finished"
        return {
            "status": "finished",
            "task_id": task["task_id"],
            "version_before": revision_before,
            "version_after": revision_after,
            "moved": task["moved"],
            "overflow": overflow,
            "pending_placed": pending_placed,
        }

    def _finish_failed(self, task, vault):
        pre = task["pre_state"]
        vault["pages"] = deepcopy(pre["pages"])
        vault["page_sizes"] = list(pre["page_sizes"])
        vault["readonly"] = set(pre["readonly"])
        vault["locked"] = set(pre["locked"])
        vault["version"] = pre["version"]
        vault["index"] = deepcopy(pre["index"])

        pending_placed = 0
        overflow = 0
        for entry in task["pending"]:
            placed = self._insert_item(
                vault["pages"],
                vault["page_sizes"],
                vault["readonly"],
                entry["item"],
                page=None,
                auto_expand=True,
                new_page_size=vault["page_size"],
            )
            overflow += placed["overflow"]
            if placed["placed"] > 0:
                pending_placed += 1

        self._recount(vault)
        task["operations"] = []
        task["after_pages"] = deepcopy(vault["pages"])
        task["version_after"] = pre["version"]
        task["moved"] = 0
        task["overflow"] = overflow
        task["pending_placed"] = pending_placed
        task["status"] = "failed"
        return {
            "status": "failed",
            "task_id": task["task_id"],
            "version_before": pre["version"],
            "version_after": pre["version"],
            "moved": 0,
            "overflow": overflow,
            "pending_placed": pending_placed,
        }

    def _compact_layout(self, pages, page_sizes, readonly, locked, selected):
        movable_pages = [page for page in selected if page not in readonly]
        gathered = []

        for page_index in movable_pages:
            page = pages[page_index]
            for slot_index, slot in enumerate(page):
                if slot is None:
                    continue
                if slot.get("item_id") in locked or slot.get("locked"):
                    continue
                gathered.append((page_index, slot_index, deepcopy(slot)))
                page[slot_index] = None

        groups = {}
        for page_index, slot_index, slot in gathered:
            groups.setdefault(slot["item_id"], []).append(
                (page_index, slot_index, slot)
            )
        for contributions in groups.values():
            contributions.sort(key=lambda row: (row[0], row[1]))

        available = []
        for page_index in movable_pages:
            for slot_index, slot in enumerate(pages[page_index]):
                if slot is None:
                    available.append((page_index, slot_index))

        operations = []
        overflow = 0
        available_index = 0

        for item_id in sorted(groups, key=lambda value: (str(value), repr(value))):
            contributions = groups[item_id]
            template = self._merged_template(contributions)
            total = sum(slot["count"] for _, _, slot in contributions)
            chunks = []
            remaining = total
            while remaining > 0:
                chunk = min(self.stack_limit, remaining)
                chunks.append(chunk)
                remaining -= chunk

            item_placements = []
            for chunk in chunks:
                if available_index >= len(available):
                    overflow += chunk
                    continue
                page_index, slot_index = available[available_index]
                available_index += 1
                pages[page_index][slot_index] = self._make_slot(template, chunk)
                item_placements.append((page_index, slot_index, chunk))

            source_index = 0
            source_left = contributions[0][2]["count"] if contributions else 0
            for destination_page, destination_slot, destination_count in item_placements:
                needed = destination_count
                while needed > 0 and source_index < len(contributions):
                    page_index, slot_index, _ = contributions[source_index]
                    take = min(source_left, needed)
                    operations.append(
                        {
                            "item_id": item_id,
                            "count": take,
                            "from": {"page": page_index, "slot": slot_index},
                            "to": {
                                "page": destination_page,
                                "slot": destination_slot,
                            },
                        }
                    )
                    source_left -= take
                    needed -= take
                    if source_left == 0:
                        source_index += 1
                        if source_index < len(contributions):
                            source_left = contributions[source_index][2]["count"]

        new_pages = []
        new_sizes = []
        new_readonly = set()
        old_to_new = {}
        locked_pages = {
            page_index
            for page_index, page in enumerate(pages)
            if any(
                slot is not None
                and (slot.get("item_id") in locked or slot.get("locked"))
                for slot in page
            )
        }
        protected = locked_pages.union(
            page_index
            for page_index in range(len(pages))
            if page_index not in movable_pages
        )
        last_protected = max(protected) if protected else -1
        for page_index, page in enumerate(pages):
            remove = (
                page_index in movable_pages
                and page_index > last_protected
                and all(slot is None for slot in page)
            )
            if remove:
                continue
            old_to_new[page_index] = len(new_pages)
            new_pages.append(page)
            new_sizes.append(page_sizes[page_index])
            if page_index in readonly:
                new_readonly.add(len(new_pages) - 1)

        if not new_pages:
            size = page_sizes[-1] if page_sizes else 10
            new_pages.append([None] * size)
            new_sizes.append(size)

        for operation in operations:
            destination = operation["to"]
            if destination["page"] in old_to_new:
                destination["page"] = old_to_new[destination["page"]]

        return (
            new_pages,
            new_sizes,
            new_readonly,
            operations,
            overflow,
        )

    def _insert_item(
        self,
        pages,
        page_sizes,
        readonly,
        item,
        page=None,
        auto_expand=False,
        new_page_size=10,
    ):
        if not isinstance(item, dict) or "item_id" not in item:
            raise ValueError("item must be a dict with item_id")
        count = int(item.get("count", 0))
        if count < 0:
            raise ValueError("count must not be negative")
        total = count
        chunks = []

        if page is not None:
            if page < 0 or page >= len(pages):
                raise IndexError("page out of range")
            if page not in readonly:
                self._fill_page(pages, page, item, total, chunks)
            placed = sum(chunk["count"] for chunk in chunks)
            overflow = total - placed
            return self._placement_result(placed, overflow, chunks)

        remaining = total
        for page_index in range(len(pages)):
            if remaining <= 0:
                break
            if page_index in readonly:
                continue
            remaining -= self._fill_page(
                pages,
                page_index,
                item,
                remaining,
                chunks,
            )

        while remaining > 0 and auto_expand:
            size = int(new_page_size)
            if size < 1:
                size = 1
            pages.append([None] * size)
            page_sizes.append(size)
            remaining -= self._fill_page(
                pages,
                len(pages) - 1,
                item,
                remaining,
                chunks,
            )

        placed = total - remaining
        return self._placement_result(placed, remaining, chunks)

    def _fill_page(self, pages, page_index, item, amount, chunks):
        filled = 0
        page = pages[page_index]
        for slot_index, slot in enumerate(page):
            if filled >= amount:
                break
            if slot is not None:
                continue
            chunk = min(self.stack_limit, amount - filled)
            page[slot_index] = self._make_slot(item, chunk)
            chunks.append(
                {
                    "page": page_index,
                    "slot": slot_index,
                    "count": chunk,
                    "item_id": item["item_id"],
                }
            )
            filled += chunk
        return filled

    @staticmethod
    def _placement_result(placed, overflow, chunks):
        return {
            "placed": placed,
            "overflow": overflow,
            "slots": [
                {"page": chunk["page"], "slot": chunk["slot"]} for chunk in chunks
            ],
            "chunks": chunks,
        }

    @staticmethod
    def _make_slot(item, count):
        slot = deepcopy(item)
        slot["count"] = count
        return slot

    @staticmethod
    def _merged_template(contributions):
        template = deepcopy(contributions[0][2])
        template.pop("count", None)
        for _, _, slot in contributions[1:]:
            for key, value in slot.items():
                if key == "count":
                    continue
                if key in ("favorite", "locked"):
                    template[key] = bool(template.get(key)) or bool(value)
                elif key not in template:
                    template[key] = deepcopy(value)
        return template

    @staticmethod
    def _selected_pages(vault, pages):
        if pages is None:
            return list(range(len(vault["pages"])))
        selected = []
        for page in pages:
            page = int(page)
            if page < 0 or page >= len(vault["pages"]):
                raise IndexError("page out of range")
            if page not in selected:
                selected.append(page)
        return sorted(selected)

    def _live_snapshot(self, vault_id):
        vault = self._vault(vault_id)
        return {
            "pages": deepcopy(vault["pages"]),
            "index": deepcopy(vault["index"]),
            "version": vault["version"],
            "compacting": False,
        }

    def _vault(self, vault_id):
        vault = self._vaults.get(vault_id)
        if vault is None:
            raise KeyError(vault_id)
        return vault

    def _recount(self, vault):
        used = sum(
            1
            for page in vault["pages"]
            for slot in page
            if slot is not None
        )
        capacity = sum(len(page) for page in vault["pages"])
        vault["index"] = {
            "used": used,
            "capacity": capacity,
            "free": capacity - used,
        }
