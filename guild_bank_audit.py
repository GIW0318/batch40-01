"""Guild bank deposits, withdrawals, limits and audit trail."""

from __future__ import annotations

import copy
import math
import threading
import uuid
from contextlib import contextmanager


class GuildBankAudit:
    def __init__(
        self,
        guild_id="guild",
        capacity=20,
        max_limit=1000,
        timezone="+08:00",
        mail_sender=None,
    ):
        self.guild_id = guild_id
        self.capacity = capacity
        self.max_limit = max_limit
        self.timezone = timezone
        self.slots = {}
        self.members = {}
        self.logs = []
        self.funds = 0
        self.requests = {}
        self.pages = 1
        self.dissolved = False
        self.mail = []
        self.asset_snapshots = []
        self.leader = None
        self.mail_sender = mail_sender
        self.daily_usage = {}
        self.open_panels = []
        self._sequence = 0
        self._dissolve_result = None
        self._state_lock = threading.RLock()
        self._slot_locks = {}

    def add_member(
        self,
        actor,
        role="member",
        can_withdraw=True,
        daily_limit=10,
        can_deposit=True,
    ):
        with self._state_lock:
            if actor in self.members:
                raise ValueError("member already exists")
            if not isinstance(daily_limit, int) or isinstance(daily_limit, bool):
                raise ValueError("daily limit must be an integer")
            if daily_limit < 0:
                raise ValueError("daily limit cannot be negative")
            self.members[actor] = {
                "name": actor,
                "role": role,
                "can_withdraw": bool(can_withdraw),
                "can_deposit": bool(can_deposit),
                "daily_limit": daily_limit,
                "permissions_version": 0,
            }
            if role in ("master", "leader") and self.leader is None:
                self.leader = actor
            return copy.deepcopy(self.members[actor])

    def add_funds(self, amount):
        with self._state_lock:
            if not isinstance(amount, int) or isinstance(amount, bool):
                raise ValueError("amount must be an integer")
            if amount < 0:
                raise ValueError("amount cannot be negative")
            before = self.funds
            self.funds += amount
            if amount:
                self._append_log(
                    actor="system",
                    action="add_funds",
                    before=before,
                    after=self.funds,
                    at=0.0,
                    amount=amount,
                )
            return self.funds

    def set_permission(self, actor, can_withdraw=None, role=None):
        def operation():
            member = self._require_member(actor)
            before = {
                "role": member["role"],
                "can_withdraw": member["can_withdraw"],
            }
            if role is not None:
                member["role"] = role
                if role in ("master", "leader") and self.leader is None:
                    self.leader = actor
            if can_withdraw is not None:
                member["can_withdraw"] = bool(can_withdraw)
            member["permissions_version"] += 1
            self._invalidate_panels(actor, member["permissions_version"])
            after = {
                "role": member["role"],
                "can_withdraw": member["can_withdraw"],
            }
            self._append_log(
                actor=actor,
                action="set_permission",
                before=before,
                after=after,
                at=0.0,
                target=actor,
            )
            return {
                "actor": actor,
                "before": copy.deepcopy(before),
                "after": copy.deepcopy(after),
                "permissions_version": member["permissions_version"],
                "duplicate": False,
            }

        return self._execute(None, "set_permission", 0.0, operation)

    def open_panel(self, actor):
        with self._state_lock:
            member = self._require_member(actor)
            panel = {
                "actor": actor,
                "role": member["role"],
                "can_withdraw": member["can_withdraw"],
                "can_deposit": member["can_deposit"],
                "daily_limit": member["daily_limit"],
                "permissions_version": member["permissions_version"],
                "valid": True,
                "invalidated": False,
            }
            self.open_panels.append(panel)
            return panel

    def deposit(
        self,
        actor,
        tab,
        page,
        quantity,
        item_id,
        bound_to=None,
        request_id=None,
        expected_version=None,
        now=0.0,
    ):
        def operation():
            self._require_active()
            member = self._require_member(actor)
            if not member["can_deposit"]:
                raise ValueError("deposit permission denied")
            self._validate_location(tab, page)
            self._validate_quantity(quantity)
            if item_id is None or item_id == "":
                raise ValueError("item_id is required")

            key = (tab, page)
            with self._lock_slots(key):
                slot = self.slots.get(key)
                current = self._slot_quantity(slot)
                version = self._slot_version(slot)
                if expected_version is not None and expected_version != version:
                    raise ValueError("stale slot version")
                if current:
                    if slot["item_id"] != item_id:
                        raise ValueError("slot contains a different item")
                    if slot.get("bound_to") != bound_to:
                        raise ValueError("slot binding does not match")

                before = current
                after = before + quantity
                if slot is None:
                    slot = {
                        "tab": tab,
                        "page": page,
                        "item_id": item_id,
                        "quantity": 0,
                        "bound_to": bound_to,
                        "version": version,
                    }
                    self.slots[key] = slot
                slot["item_id"] = item_id
                slot["quantity"] = after
                slot["bound_to"] = bound_to
                slot["version"] = version + 1
                self._append_log(
                    actor=actor,
                    action="deposit",
                    tab=tab,
                    page=page,
                    before=before,
                    after=after,
                    at=now,
                    item_id=item_id,
                    item_snapshot=self._item_snapshot(
                        item_id, quantity, bound_to
                    ),
                )
                return {
                    "action": "deposit",
                    "tab": tab,
                    "page": page,
                    "item_id": item_id,
                    "quantity": after,
                    "before": before,
                    "after": after,
                    "version": slot["version"],
                    "duplicate": False,
                }

        return self._execute(request_id, "deposit", now, operation)

    def withdraw(
        self,
        actor,
        tab,
        page,
        quantity,
        request_id=None,
        expected_version=None,
        now=0.0,
    ):
        def operation():
            self._require_active()
            member = self._require_member(actor)
            if not member["can_withdraw"]:
                raise ValueError("withdraw permission denied")
            self._validate_location(tab, page)
            self._validate_quantity(quantity)

            key = (tab, page)
            with self._lock_slots(key):
                slot = self.slots.get(key)
                version = self._slot_version(slot)
                if expected_version is not None and expected_version != version:
                    raise ValueError("stale slot version")
                before = self._slot_quantity(slot)
                if before <= 0:
                    raise ValueError("slot is empty")
                if quantity > before:
                    raise ValueError("not enough items in slot")
                bound_to = slot.get("bound_to")
                if bound_to is not None and bound_to != actor:
                    raise ValueError("bound item belongs to another member")

                usage = self._daily_usage(actor, now)
                if usage + quantity > member["daily_limit"]:
                    raise ValueError("daily withdrawal limit exceeded")

                item_id = slot["item_id"]
                item_snapshot = self._item_snapshot(item_id, quantity, bound_to)
                after = before - quantity
                slot["quantity"] = after
                slot["version"] = version + 1
                if after == 0:
                    slot["item_id"] = None
                    slot["bound_to"] = None
                self._set_daily_usage(actor, now, usage + quantity)
                self._append_log(
                    actor=actor,
                    action="withdraw",
                    tab=tab,
                    page=page,
                    before=before,
                    after=after,
                    at=now,
                    item_id=item_id,
                    item_snapshot=item_snapshot,
                )
                return {
                    "action": "withdraw",
                    "tab": tab,
                    "page": page,
                    "item_id": item_id,
                    "quantity": after,
                    "before": before,
                    "after": after,
                    "withdrawn": quantity,
                    "version": slot["version"],
                    "duplicate": False,
                }

        return self._execute(request_id, "withdraw", now, operation)

    def set_limit(self, actor, target, limit, request_id=None):
        def operation():
            member = self._require_member(actor)
            target_member = self._require_member(target)
            if member["role"] not in ("master", "leader"):
                raise ValueError("only the guild master can change limits")
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise ValueError("limit must be an integer")
            if limit < 0 or limit > self.max_limit:
                raise ValueError("limit exceeds the configured maximum")

            before = target_member["daily_limit"]
            target_member["daily_limit"] = limit
            target_member["permissions_version"] += 1
            self._invalidate_panels(target, target_member["permissions_version"])
            self._append_log(
                actor=actor,
                action="set_limit",
                before=before,
                after=limit,
                at=0.0,
                target=target,
            )
            return {
                "target": target,
                "limit": limit,
                "before": before,
                "after": limit,
                "duplicate": False,
            }

        return self._execute(request_id, "set_limit", 0.0, operation)

    def move(
        self,
        actor,
        from_tab,
        from_page,
        to_tab,
        to_page,
        quantity,
        request_id=None,
        now=0.0,
    ):
        def operation():
            self._require_active()
            member = self._require_member(actor)
            if not member["can_withdraw"] or not member["can_deposit"]:
                raise ValueError("move permission denied")
            self._validate_location(from_tab, from_page)
            self._validate_location(to_tab, to_page)
            self._validate_quantity(quantity)
            source_key = (from_tab, from_page)
            target_key = (to_tab, to_page)
            if source_key == target_key:
                raise ValueError("source and destination are the same slot")

            with self._lock_slots(source_key, target_key):
                source = self.slots.get(source_key)
                target = self.slots.get(target_key)
                source_before = self._slot_quantity(source)
                if source_before <= 0:
                    raise ValueError("source slot is empty")
                if quantity > source_before:
                    raise ValueError("not enough items to move")
                item_id = source["item_id"]
                bound_to = source.get("bound_to")
                if bound_to is not None and bound_to != actor:
                    raise ValueError("bound item belongs to another member")

                target_before = self._slot_quantity(target)
                if target_before:
                    if target["item_id"] != item_id:
                        raise ValueError("destination contains a different item")
                    if target.get("bound_to") != bound_to:
                        raise ValueError("destination binding does not match")

                source_after = source_before - quantity
                target_after = target_before + quantity
                source["quantity"] = source_after
                source["version"] = self._slot_version(source) + 1
                if source_after == 0:
                    source["item_id"] = None
                    source["bound_to"] = None

                if target is None:
                    target = {
                        "tab": to_tab,
                        "page": to_page,
                        "item_id": item_id,
                        "quantity": 0,
                        "bound_to": bound_to,
                        "version": 0,
                    }
                    self.slots[target_key] = target
                target["item_id"] = item_id
                target["quantity"] = target_after
                target["bound_to"] = bound_to
                target["version"] = self._slot_version(target) + 1

                self._append_log(
                    actor=actor,
                    action="move",
                    tab=from_tab,
                    page=from_page,
                    before=source_before,
                    after=source_after,
                    at=now,
                    item_id=item_id,
                    item_snapshot=self._item_snapshot(
                        item_id, quantity, bound_to
                    ),
                    from_tab=from_tab,
                    from_page=from_page,
                    to_tab=to_tab,
                    to_page=to_page,
                    from_before=source_before,
                    from_after=source_after,
                    to_before=target_before,
                    to_after=target_after,
                    source_version=source["version"],
                    target_version=target["version"],
                )
                return {
                    "action": "move",
                    "from_tab": from_tab,
                    "from_page": from_page,
                    "to_tab": to_tab,
                    "to_page": to_page,
                    "item_id": item_id,
                    "quantity": quantity,
                    "source_quantity": source_after,
                    "target_quantity": target_after,
                    "duplicate": False,
                }

        return self._execute(request_id, "move", now, operation)

    def charge_repair(self, actor, amount, request_id=None, now=0.0):
        def operation():
            self._require_active()
            member = self._require_member(actor)
            if not member["can_withdraw"]:
                raise ValueError("repair charge permission denied")
            if not isinstance(amount, int) or isinstance(amount, bool):
                raise ValueError("amount must be an integer")
            if amount <= 0:
                raise ValueError("amount must be positive")
            if amount > self.funds:
                raise ValueError("insufficient guild funds")

            before = self.funds
            self.funds -= amount
            self._append_log(
                actor=actor,
                action="repair",
                before=before,
                after=self.funds,
                at=now,
                amount=amount,
            )
            return {
                "charged": amount,
                "before": before,
                "after": self.funds,
                "duplicate": False,
            }

        return self._execute(request_id, "charge_repair", now, operation)

    def expand(self, actor, pages, now=0.0):
        def operation():
            member = self._require_member(actor)
            if member["role"] not in ("master", "leader"):
                raise ValueError("only the guild master can expand the bank")
            if not isinstance(pages, int) or isinstance(pages, bool):
                raise ValueError("pages must be an integer")
            if pages < self.pages:
                raise ValueError("bank cannot shrink")
            before = self.pages
            self.pages = pages
            self._append_log(
                actor=actor,
                action="expand",
                before=before,
                after=pages,
                at=now,
            )
            return {"before": before, "after": pages, "pages": pages}

        return self._execute(None, "expand", now, operation)

    def log(self, tab=None, page=None):
        with self._state_lock:
            entries = []
            for entry in self.logs:
                if tab is None and page is None:
                    entries.append(copy.deepcopy(entry))
                elif self._log_matches(entry, tab, page):
                    entries.append(copy.deepcopy(entry))
            return entries

    def slot_state(self, tab, page):
        with self._state_lock:
            self._validate_location(tab, page)
            slot = self.slots.get((tab, page))
            if slot is None:
                return {
                    "tab": tab,
                    "page": page,
                    "item_id": None,
                    "quantity": 0,
                    "bound_to": None,
                    "version": 0,
                    "empty": True,
                }
            result = copy.deepcopy(slot)
            result["empty"] = result["quantity"] <= 0
            return result

    def guild_funds(self):
        with self._state_lock:
            return self.funds

    def page_count(self):
        with self._state_lock:
            return self.pages

    def asset_snapshots_list(self):
        with self._state_lock:
            return copy.deepcopy(self.asset_snapshots)

    def dissolve(self, actor, request_id=None, now=0.0):
        def operation():
            if self.dissolved and self._dissolve_result is not None:
                result = copy.deepcopy(self._dissolve_result)
                result["duplicate"] = True
                return result

            member = self._require_member(actor)
            if member["role"] not in ("master", "leader") and actor != self.leader:
                raise ValueError("only the guild master can dissolve the guild")

            assets = []
            for key in sorted(self.slots, key=lambda value: (str(value[0]), value[1])):
                slot = self.slots[key]
                if self._slot_quantity(slot) <= 0:
                    continue
                assets.append(
                    {
                        "tab": slot["tab"],
                        "page": slot["page"],
                        "item_id": slot["item_id"],
                        "quantity": slot["quantity"],
                        "bound_to": slot.get("bound_to"),
                        "version": slot["version"],
                    }
                )

            snapshot_id = uuid.uuid4().hex
            snapshot = {
                "snapshot_id": snapshot_id,
                "guild_id": self.guild_id,
                "actor": actor,
                "created_at": float(now),
                "timezone": self.timezone,
                "assets": copy.deepcopy(assets),
                "item_count": sum(asset["quantity"] for asset in assets),
            }
            self.asset_snapshots.append(snapshot)

            recipient = self.leader or actor
            mail_entries = []
            for asset in assets:
                entry = {
                    "mail_id": uuid.uuid4().hex,
                    "snapshot_id": snapshot_id,
                    "to": recipient,
                    "from": self.guild_id,
                    "item_id": asset["item_id"],
                    "quantity": asset["quantity"],
                    "bound_to": asset["bound_to"],
                    "status": "queued",
                    "attempts": 0,
                    "created_at": float(now),
                }
                self.mail.append(entry)
                mail_entries.append(entry)

            total_before = sum(asset["quantity"] for asset in assets)
            for slot in self.slots.values():
                if self._slot_quantity(slot) > 0:
                    slot["quantity"] = 0
                    slot["item_id"] = None
                    slot["bound_to"] = None
                    slot["version"] = self._slot_version(slot) + 1

            self.dissolved = True
            self._append_log(
                actor=actor,
                action="dissolve",
                before=total_before,
                after=0,
                at=now,
                snapshot_id=snapshot_id,
                item_count=len(assets),
            )
            for entry in mail_entries:
                self._try_deliver(entry, now)

            result = {
                "dissolved": True,
                "mailed": len(mail_entries),
                "snapshot_id": snapshot_id,
                "duplicate": False,
            }
            self._dissolve_result = copy.deepcopy(result)
            return result

        return self._execute(request_id, "dissolve", now, operation)

    def mail_queue(self):
        with self._state_lock:
            return copy.deepcopy(self.mail)

    def retry_mail(self, now=0.0):
        with self._state_lock:
            sent = 0
            failed = 0
            for entry in self.mail:
                if entry["status"] == "sent":
                    continue
                if self._try_deliver(entry, now):
                    sent += 1
                else:
                    failed += 1
            return {"sent": sent, "failed": failed}

    def _execute(self, request_id, action, now, operation):
        with self._state_lock:
            if request_id is not None:
                previous = self.requests.get(request_id)
                if previous and previous.get("status") == "completed":
                    result = copy.deepcopy(previous["result"])
                    result["duplicate"] = True
                    return result

            snapshot = self._capture_state()
            if request_id is not None:
                self.requests[request_id] = {
                    "status": "pending",
                    "action": action,
                    "started_at": float(now),
                }
            try:
                result = operation()
            except Exception as exc:
                self._restore_state(snapshot)
                if request_id is not None:
                    self.requests[request_id] = {
                        "status": "rolled_back",
                        "action": action,
                        "started_at": float(now),
                        "error": str(exc),
                    }
                raise

            if request_id is not None:
                self.requests[request_id] = {
                    "status": "completed",
                    "action": action,
                    "started_at": float(now),
                    "result": copy.deepcopy(result),
                }
            return result

    def _capture_state(self):
        return {
            "slots": copy.deepcopy(self.slots),
            "members": copy.deepcopy(self.members),
            "logs": copy.deepcopy(self.logs),
            "funds": self.funds,
            "pages": self.pages,
            "dissolved": self.dissolved,
            "mail": copy.deepcopy(self.mail),
            "asset_snapshots": copy.deepcopy(self.asset_snapshots),
            "leader": self.leader,
            "daily_usage": copy.deepcopy(self.daily_usage),
            "open_panels": copy.deepcopy(self.open_panels),
            "sequence": self._sequence,
            "dissolve_result": copy.deepcopy(self._dissolve_result),
        }

    def _restore_state(self, snapshot):
        self.slots = snapshot["slots"]
        self.members = snapshot["members"]
        self.logs = snapshot["logs"]
        self.funds = snapshot["funds"]
        self.pages = snapshot["pages"]
        self.dissolved = snapshot["dissolved"]
        self.mail = snapshot["mail"]
        self.asset_snapshots = snapshot["asset_snapshots"]
        self.leader = snapshot["leader"]
        self.daily_usage = snapshot["daily_usage"]
        self.open_panels = snapshot["open_panels"]
        self._sequence = snapshot["sequence"]
        self._dissolve_result = snapshot["dissolve_result"]

    @contextmanager
    def _lock_slots(self, *keys):
        locks = [self._slot_locks.setdefault(key, threading.RLock()) for key in keys]
        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    def _append_log(self, actor, action, before, after, at, **details):
        self._sequence += 1
        entry = {
            "seq": self._sequence,
            "guild_id": self.guild_id,
            "actor": actor,
            "action": action,
            "at": float(at),
            "timezone": self.timezone,
            "before": copy.deepcopy(before),
            "after": copy.deepcopy(after),
        }
        entry.update(copy.deepcopy(details))
        self.logs.append(entry)

    def _log_matches(self, entry, tab, page):
        locations = [
            (entry.get("tab"), entry.get("page")),
            (entry.get("from_tab"), entry.get("from_page")),
            (entry.get("to_tab"), entry.get("to_page")),
        ]
        for entry_tab, entry_page in locations:
            if tab is not None and entry_tab != tab:
                continue
            if page is not None and entry_page != page:
                continue
            if tab is not None or page is not None:
                return True
        return False

    def _require_member(self, actor):
        member = self.members.get(actor)
        if member is None:
            raise ValueError("unknown member")
        return member

    def _require_active(self):
        if self.dissolved:
            raise ValueError("guild bank is dissolved")

    def _validate_location(self, tab, page):
        if tab is None or tab == "":
            raise ValueError("tab is required")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValueError("page must be a positive integer")

    def _validate_quantity(self, quantity):
        if not isinstance(quantity, int) or isinstance(quantity, bool):
            raise ValueError("quantity must be an integer")
        if quantity <= 0:
            raise ValueError("quantity must be positive")

    def _slot_quantity(self, slot):
        if slot is None:
            return 0
        return int(slot.get("quantity", 0))

    def _slot_version(self, slot):
        if slot is None:
            return 0
        return int(slot.get("version", 0))

    def _item_snapshot(self, item_id, quantity, bound_to):
        return {
            "item_id": item_id,
            "quantity": quantity,
            "bound_to": bound_to,
        }

    def _daily_key(self, actor, now):
        offset_seconds = self._timezone_offset_seconds()
        day = math.floor((float(now) + offset_seconds) / 86400)
        return actor, day

    def _daily_usage(self, actor, now):
        return int(self.daily_usage.get(self._daily_key(actor, now), 0))

    def _set_daily_usage(self, actor, now, usage):
        self.daily_usage[self._daily_key(actor, now)] = int(usage)

    def _timezone_offset_seconds(self):
        value = self.timezone
        if value in (None, "", "UTC", "Z"):
            return 0
        sign = 1
        if value[0] in "+-":
            if value[0] == "-":
                sign = -1
            value = value[1:]
        parts = value.split(":")
        try:
            hours = int(parts[0])
            minutes = int(parts[1]) if len(parts) > 1 else 0
        except (TypeError, ValueError, IndexError):
            return 0
        return sign * (hours * 3600 + minutes * 60)

    def _invalidate_panels(self, actor, permissions_version):
        for panel in self.open_panels:
            if panel["actor"] == actor:
                panel["valid"] = False
                panel["invalidated"] = True
                panel["permissions_version"] = permissions_version

    def _try_deliver(self, entry, now):
        entry["attempts"] += 1
        try:
            if self.mail_sender is not None:
                accepted = self.mail_sender(copy.deepcopy(entry))
                if accepted is False:
                    raise RuntimeError("mail sender rejected the item")
            entry["status"] = "sent"
            entry["sent_at"] = float(now)
            entry.pop("last_error", None)
            return True
        except Exception as exc:
            entry["status"] = "failed"
            entry["last_error"] = str(exc)
            return False
