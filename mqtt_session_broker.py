"""MQTT session broker with QoS, retained, will and alias bookkeeping."""


class MqttSessionBroker:
    """In-memory broker session layer."""

    def __init__(self, inflight_limit=10, keepalive_grace=1.5):
        raise NotImplementedError()

    def connect(self, client_id, clean_session=True, keepalive=60, will=None, now=0):
        raise NotImplementedError()

    def subscribe(self, client_id, topic_filter, qos=0, now=0):
        raise NotImplementedError()

    def unsubscribe(self, client_id, topic_filter):
        raise NotImplementedError()

    def publish(self, client_id, topic, payload=b"", qos=0, retain=False,
                packet_id=None, now=0):
        raise NotImplementedError()

    def puback(self, client_id, packet_id, now=0):
        raise NotImplementedError()

    def pubrec(self, client_id, packet_id, now=0):
        raise NotImplementedError()

    def pubrel(self, client_id, packet_id, now=0):
        raise NotImplementedError()

    def pubcomp(self, client_id, packet_id, now=0):
        raise NotImplementedError()

    def poll(self, client_id, max_messages=None, now=0):
        raise NotImplementedError()

    def disconnect(self, client_id, abnormal=False, now=0):
        raise NotImplementedError()

    def tick(self, now=0):
        raise NotImplementedError()

    def set_topic_alias(self, client_id, alias, topic):
        raise NotImplementedError()

    def resolve_alias(self, client_id, alias):
        raise NotImplementedError()

    def export_state(self):
        raise NotImplementedError()

    def load_state(self, state):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
