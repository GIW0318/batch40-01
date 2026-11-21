"""instance_registry_broker."""

import copy


class InstanceRegistryBroker:
    """Instance registry broker.

    Instances have status active, grace or closed.  acquire(party_id, members,
    difficulty, now) returns status allocated, existing, queued or
    difficulty_mismatch, and an allocated result includes members and difficulty; a party has at most one active-or-grace instance and a
    different difficulty is never silently reused.  Queued parties are promoted in
    order.  release(instance_id, party_id, member_id=None, now) refuses while
    another member is online; the last release puts the instance into grace with
    reclaim_at = now + grace.  heartbeat refreshes last_seen, revives a grace
    record when members are online, and rejects members outside the roster.
    reconnect requires the exact roster and returns status reconnected with the
    same instance and progress, gone once now >= reclaim_at, or roster_mismatch.
    transfer_leader may replace the roster.  capacity returns assigned
    (active + grace), queued, available and capacity.  close returns the broadcast
    member list before freeing the slot.  reap(now) closes records whose heartbeat
    is older than heartbeat_timeout and grace records whose reclaim_at is reached
    (now >= reclaim_at),
    then promotes queued parties; it returns closed and promoted lists.  An
    instance id is reused only when no live record references it and a stale party
    must never see the new owner's progress.  record_progress appends boss ids.
    export_state/load_state preserve reclaim marks so a restart can finish
    reaping.  Reconnect statuses include reconnected, gone, party_mismatch and
    roster_mismatch; a refused release includes the online member list; a successful
    last release returns status grace with reclaim_at; reap returns closed and
    promoted.
    """

    def __init__(self, capacity=4, heartbeat_timeout=30.0, grace=10.0, id_space=8):
        self._capacity = capacity
        self._heartbeat_timeout = heartbeat_timeout
        self._grace = grace
        self._id_space = id_space
        self._next_id = 0
        self.instances = {}
        self.parties = {}
        self._queue = []
        self._reclaim_marks = {}

    def _allocate_id(self):
        for _ in range(self._id_space):
            candidate = self._next_id % self._id_space
            self._next_id += 1
            if candidate not in self.instances:
                return candidate
        return None

    def _new_record(self, instance_id, party_id, members, difficulty, now):
        return {
            "instance_id": instance_id,
            "party_id": party_id,
            "leader": members[0] if members else None,
            "members": list(members),
            "difficulty": difficulty,
            "status": "active",
            "online": list(members),
            "progress": [],
            "last_seen": now,
            "reclaim_at": None,
        }

    def _close_record(self, record):
        instance_id = record["instance_id"]
        record["status"] = "closed"
        self.instances.pop(instance_id, None)
        if self.parties.get(record["party_id"]) == instance_id:
            del self.parties[record["party_id"]]
        self._reclaim_marks.pop(instance_id, None)

    def acquire(self, party_id, members, difficulty='normal', now=0.0):
        if party_id in self.parties:
            record = self.instances[self.parties[party_id]]
            if record["difficulty"] != difficulty:
                return {
                    "status": "difficulty_mismatch",
                    "instance_id": record["instance_id"],
                    "active_difficulty": record["difficulty"],
                }
            record["last_seen"] = now
            return {"status": "existing", "instance_id": record["instance_id"]}
        for index, entry in enumerate(self._queue):
            if entry["party_id"] == party_id:
                return {"status": "queued", "position": index + 1}
        if len(self.instances) < self._capacity:
            instance_id = self._allocate_id()
            if instance_id is not None:
                record = self._new_record(instance_id, party_id, members, difficulty, now)
                self.instances[instance_id] = record
                self.parties[party_id] = instance_id
                return {
                    "status": "allocated",
                    "instance_id": instance_id,
                    "members": list(members),
                    "difficulty": difficulty,
                }
        self._queue.append({
            "party_id": party_id,
            "members": list(members),
            "difficulty": difficulty,
            "queued_at": now,
        })
        return {"status": "queued", "position": len(self._queue)}

    def release(self, instance_id, party_id, member_id=None, now=0.0):
        record = self.instances.get(instance_id)
        if record is None or record["party_id"] != party_id:
            return {"released": False, "status": "gone", "online": []}
        if member_id is not None:
            record["online"] = [m for m in record["online"] if m != member_id]
        else:
            record["online"] = []
        if record["online"]:
            return {"released": False, "online": list(record["online"])}
        if record["status"] != "grace":
            record["status"] = "grace"
            record["reclaim_at"] = now + self._grace
            self._reclaim_marks[instance_id] = record["reclaim_at"]
        return {"released": True, "status": "grace", "reclaim_at": record["reclaim_at"]}

    def heartbeat(self, instance_id, party_id, members, now=0.0):
        record = self.instances.get(instance_id)
        if record is None or record["party_id"] != party_id:
            return {"status": "gone"}
        roster = set(record["members"])
        if any(member not in roster for member in members):
            return {"status": "roster_mismatch"}
        record["last_seen"] = now
        for member in members:
            if member not in record["online"]:
                record["online"].append(member)
        if record["status"] == "grace" and record["online"]:
            record["status"] = "active"
            record["reclaim_at"] = None
            self._reclaim_marks.pop(instance_id, None)
        return {"status": record["status"]}

    def reconnect(self, party_id, instance_id, members, now=0.0):
        record = self.instances.get(instance_id)
        if record is None:
            return {"status": "gone"}
        if record["party_id"] != party_id:
            return {"status": "party_mismatch"}
        if record["status"] == "grace" and now >= record["reclaim_at"]:
            self._close_record(record)
            return {"status": "gone"}
        if set(members) != set(record["members"]):
            return {"status": "roster_mismatch"}
        record["status"] = "active"
        record["online"] = list(record["members"])
        record["last_seen"] = now
        record["reclaim_at"] = None
        self._reclaim_marks.pop(instance_id, None)
        return {
            "status": "reconnected",
            "instance_id": instance_id,
            "progress": list(record["progress"]),
        }

    def transfer_leader(self, instance_id, new_leader, new_members=None):
        record = self.instances.get(instance_id)
        if record is None:
            return {"status": "gone"}
        record["leader"] = new_leader
        if new_members is not None:
            record["members"] = list(new_members)
            allowed = set(new_members)
            record["online"] = [m for m in record["online"] if m in allowed]
        return {
            "status": "transferred",
            "leader": new_leader,
            "members": list(record["members"]),
        }

    def record_progress(self, instance_id, boss_id):
        record = self.instances.get(instance_id)
        if record is None:
            return {"status": "gone"}
        record["progress"].append(boss_id)
        return {"status": "recorded", "progress": list(record["progress"])}

    def close(self, instance_id, now=0.0):
        record = self.instances.get(instance_id)
        if record is None:
            return {"status": "gone"}
        broadcasts = list(record["members"])
        self._close_record(record)
        return {"status": "closed", "instance_id": instance_id, "broadcasts": broadcasts}

    def reap(self, now):
        closed = []
        promoted = []
        for record in list(self.instances.values()):
            if record["status"] == "grace" and now >= record["reclaim_at"]:
                self._close_record(record)
                closed.append(record["instance_id"])
            elif (
                record["status"] == "active"
                and now - record["last_seen"] > self._heartbeat_timeout
            ):
                self._close_record(record)
                closed.append(record["instance_id"])
        while self._queue and len(self.instances) < self._capacity:
            entry = self._queue.pop(0)
            if entry["party_id"] in self.parties:
                continue
            instance_id = self._allocate_id()
            if instance_id is None:
                self._queue.insert(0, entry)
                break
            record = self._new_record(
                instance_id, entry["party_id"], entry["members"], entry["difficulty"], now
            )
            self.instances[instance_id] = record
            self.parties[entry["party_id"]] = instance_id
            promoted.append(entry["party_id"])
        return {"closed": closed, "promoted": promoted}

    def capacity(self):
        assigned = len(self.instances)
        return {
            "assigned": assigned,
            "queued": len(self._queue),
            "available": self._capacity - assigned,
            "capacity": self._capacity,
        }

    def export_state(self):
        return copy.deepcopy({
            "capacity": self._capacity,
            "heartbeat_timeout": self._heartbeat_timeout,
            "grace": self._grace,
            "id_space": self._id_space,
            "next_id": self._next_id,
            "instances": self.instances,
            "parties": self.parties,
            "queue": self._queue,
            "reclaim_marks": self._reclaim_marks,
        })

    def load_state(self, state):
        self._capacity = state["capacity"]
        self._heartbeat_timeout = state["heartbeat_timeout"]
        self._grace = state["grace"]
        self._id_space = state["id_space"]
        self._next_id = state["next_id"]
        self.instances = copy.deepcopy(state["instances"])
        self.parties = dict(state["parties"])
        self._queue = copy.deepcopy(state["queue"])
        self._reclaim_marks = dict(state["reclaim_marks"])
