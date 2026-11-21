"""MQTT session broker with QoS, retained, will and alias bookkeeping."""

import copy


def _topic_matches(topic_filter, topic):
    if topic.startswith("$") and not topic_filter.startswith("$"):
        return False
    filter_levels = topic_filter.split("/")
    topic_levels = topic.split("/")
    for index, level in enumerate(filter_levels):
        if level == "#":
            return True
        if index >= len(topic_levels):
            return False
        if level != "+" and level != topic_levels[index]:
            return False
    return len(filter_levels) == len(topic_levels)


def _new_session(clean):
    return {
        "clean": clean,
        "connected": False,
        "keepalive": 60,
        "last_seen": 0,
        "will": None,
        "subscriptions": [],
        "queue": [],
        "inflight": {},
        "next_packet_id": 1,
    }


class MqttSessionBroker:
    """In-memory broker session layer."""

    def __init__(self, inflight_limit=10, keepalive_grace=1.5):
        self.inflight_limit = inflight_limit
        self.keepalive_grace = keepalive_grace
        self.sessions = {}
        self.retained = {}
        self.delivered = 0
        self.aliases = {}
        self.pending_qos1 = {}
        self.pending_qos2 = {}

    def connect(self, client_id, clean_session=True, keepalive=60, will=None, now=0):
        existing = self.sessions.get(client_id)
        session_present = (
            not clean_session
            and existing is not None
            and not existing["clean"]
        )
        if clean_session or existing is None:
            session = _new_session(clean_session)
            self.sessions[client_id] = session
        else:
            session = existing
            session["clean"] = clean_session
        session["connected"] = True
        session["keepalive"] = keepalive
        session["last_seen"] = now
        session["will"] = copy.deepcopy(will) if will else None
        return {"session_present": session_present}

    def subscribe(self, client_id, topic_filter, qos=0, now=0):
        session = self.sessions[client_id]
        session["last_seen"] = now
        for subscription in session["subscriptions"]:
            if subscription[0] == topic_filter:
                subscription[1] = qos
                break
        else:
            session["subscriptions"].append([topic_filter, qos])
        for topic, message in self.retained.items():
            if _topic_matches(topic_filter, topic):
                self._enqueue(session, topic, message["payload"],
                              min(qos, message["qos"]), retain=True)

    def unsubscribe(self, client_id, topic_filter):
        session = self.sessions.get(client_id)
        if session is None:
            return
        session["subscriptions"] = [
            subscription for subscription in session["subscriptions"]
            if subscription[0] != topic_filter
        ]

    def publish(self, client_id, topic, payload=b"", qos=0, retain=False,
                packet_id=None, now=0):
        session = self.sessions.get(client_id)
        if session is not None:
            session["last_seen"] = now
        if qos > 0:
            if packet_id is None:
                raise ValueError("packet_id is required for qos > 0")
            if packet_id in self.pending_qos1.get(client_id, ()):
                raise ValueError("packet_id reused before ack")
            if packet_id in self.pending_qos2.get(client_id, {}):
                raise ValueError("packet_id reused before ack")
        if qos == 2:
            self.pending_qos2.setdefault(client_id, {})[packet_id] = {
                "message": {"topic": topic, "payload": payload, "qos": qos},
                "released": False,
            }
            deliveries = 0
        else:
            if qos == 1:
                self.pending_qos1.setdefault(client_id, set()).add(packet_id)
            deliveries = self._route(topic, payload, qos)
        if retain:
            if payload:
                self.retained[topic] = {
                    "topic": topic, "payload": payload, "qos": qos,
                }
            else:
                self.retained.pop(topic, None)
        return {"deliveries": deliveries}

    def puback(self, client_id, packet_id, now=0):
        session = self.sessions.get(client_id)
        if session is not None:
            session["last_seen"] = now
            session["inflight"].pop(packet_id, None)
        pending = self.pending_qos1.get(client_id)
        if pending is not None:
            pending.discard(packet_id)

    def pubrec(self, client_id, packet_id, now=0):
        session = self.sessions.get(client_id)
        if session is not None:
            session["last_seen"] = now
            message = session["inflight"].get(packet_id)
            if message is not None:
                message["pubrec"] = True

    def pubrel(self, client_id, packet_id, now=0):
        session = self.sessions.get(client_id)
        if session is not None:
            session["last_seen"] = now
        entry = self.pending_qos2.get(client_id, {}).get(packet_id)
        if entry is not None:
            if not entry["released"]:
                entry["released"] = True
                message = entry["message"]
                self._route(message["topic"], message["payload"],
                            message["qos"])
            return
        if session is not None:
            message = session["inflight"].get(packet_id)
            if message is not None and message.get("qos") == 2:
                del session["inflight"][packet_id]

    def pubcomp(self, client_id, packet_id, now=0):
        session = self.sessions.get(client_id)
        if session is not None:
            session["last_seen"] = now
        pending = self.pending_qos2.get(client_id)
        if pending is not None:
            pending.pop(packet_id, None)

    def poll(self, client_id, max_messages=None, now=0):
        session = self.sessions.get(client_id)
        if session is None:
            return []
        session["last_seen"] = now
        capacity = self.inflight_limit - len(session["inflight"])
        if max_messages is not None:
            capacity = min(capacity, max_messages)
        messages = []
        while session["queue"] and len(messages) < capacity:
            message = session["queue"].pop(0)
            delivery = {
                "topic": message["topic"],
                "payload": message["payload"],
                "qos": message["qos"],
                "retain": message.get("retain", False),
                "packet_id": None,
            }
            if message["qos"] > 0:
                packet_id = self._next_packet_id(session)
                delivery["packet_id"] = packet_id
                session["inflight"][packet_id] = delivery
            messages.append(delivery)
        return messages

    def disconnect(self, client_id, abnormal=False, now=0):
        session = self.sessions.get(client_id)
        if session is None:
            return
        session["connected"] = False
        session["last_seen"] = now
        will = session["will"]
        session["will"] = None
        if abnormal and will:
            self._route(will["topic"], will["payload"], will.get("qos", 0))
            if will.get("retain") and will["payload"]:
                self.retained[will["topic"]] = {
                    "topic": will["topic"],
                    "payload": will["payload"],
                    "qos": will.get("qos", 0),
                }
        if session["clean"]:
            session["subscriptions"] = []
            session["queue"] = []
            session["inflight"] = {}

    def tick(self, now=0):
        expired = []
        for client_id, session in self.sessions.items():
            if not session["connected"]:
                continue
            deadline = session["keepalive"] * self.keepalive_grace
            if now - session["last_seen"] > deadline:
                expired.append(client_id)
        for client_id in expired:
            self.disconnect(client_id, abnormal=True, now=now)
        return expired

    def set_topic_alias(self, client_id, alias, topic):
        self.aliases.setdefault(client_id, {})[alias] = topic

    def resolve_alias(self, client_id, alias):
        mapping = self.aliases.get(client_id, {})
        if alias not in mapping:
            raise ValueError("unknown topic alias %r for %r" % (alias, client_id))
        return mapping[alias]

    def export_state(self):
        return {
            "sessions": copy.deepcopy(self.sessions),
            "retained": copy.deepcopy(self.retained),
            "delivered": self.delivered,
            "aliases": copy.deepcopy(self.aliases),
        }

    def load_state(self, state):
        self.sessions = copy.deepcopy(state.get("sessions", {}))
        self.retained = copy.deepcopy(state.get("retained", {}))
        self.delivered = state.get("delivered", 0)
        self.aliases = copy.deepcopy(state.get("aliases", {}))

    def snapshot(self):
        return {
            "delivered": self.delivered,
            "retained": {
                topic: message["payload"]
                for topic, message in self.retained.items()
            },
            "sessions": {
                client_id: {
                    "connected": session["connected"],
                    "inflight": len(session["inflight"]),
                    "queued": len(session["queue"]),
                    "subscriptions": [
                        topic_filter
                        for topic_filter, _ in session["subscriptions"]
                    ],
                }
                for client_id, session in self.sessions.items()
            },
        }

    def _enqueue(self, session, topic, payload, qos, retain=False):
        session["queue"].append({
            "topic": topic,
            "payload": payload,
            "qos": qos,
            "retain": retain,
        })
        self.delivered += 1

    def _route(self, topic, payload, qos):
        deliveries = 0
        for session in self.sessions.values():
            best = None
            for topic_filter, subscription_qos in session["subscriptions"]:
                if _topic_matches(topic_filter, topic):
                    effective = min(qos, subscription_qos)
                    best = effective if best is None else max(best, effective)
            if best is not None:
                self._enqueue(session, topic, payload, best)
                deliveries += 1
        return deliveries

    def _next_packet_id(self, session):
        for _ in range(65535):
            packet_id = session["next_packet_id"]
            session["next_packet_id"] = packet_id % 65535 + 1
            if packet_id not in session["inflight"]:
                return packet_id
        raise RuntimeError("no packet ids available")
