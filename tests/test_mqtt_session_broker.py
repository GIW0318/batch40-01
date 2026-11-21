import pytest

import mqtt_session_broker as msb


def broker(**kwargs):
    return msb.MqttSessionBroker(**kwargs)


def subscriber(b, client_id="sub", topic="cmd/#", qos=1, clean=True, now=0):
    b.connect(client_id, clean_session=clean, now=now)
    b.subscribe(client_id, topic, qos=qos, now=now)


def publisher(b, client_id="pub", now=0):
    b.connect(client_id, clean_session=True, now=now)


def test_qos2_pubrel_replay_delivers_exactly_once():
    b = broker()
    subscriber(b, qos=2)
    publisher(b)
    result = b.publish("pub", "cmd/open", b"go", qos=2, packet_id=7, now=0)
    assert result["deliveries"] == 0
    assert b.poll("sub") == []
    b.pubrel("pub", 7, now=1)
    first = b.poll("sub")
    assert len(first) == 1
    assert first[0]["payload"] == b"go"
    b.pubrel("pub", 7, now=2)
    assert b.poll("sub") == []
    assert b.snapshot()["delivered"] == 1


def test_qos1_ack_clears_inflight_window():
    b = broker()
    subscriber(b, qos=1)
    publisher(b)
    b.publish("pub", "cmd/a", b"one", qos=1, packet_id=1, now=0)
    messages = b.poll("sub")
    assert len(messages) == 1
    assert b.snapshot()["sessions"]["sub"]["inflight"] == 1
    b.puback("sub", messages[0]["packet_id"], now=1)
    assert b.snapshot()["sessions"]["sub"]["inflight"] == 0


def test_fresh_connect_without_clean_session_reports_no_session():
    b = broker()
    result = b.connect("dev", clean_session=False, keepalive=60, now=0)
    assert result["session_present"] is False


def test_reconnect_restores_session_and_subscriptions():
    b = broker()
    subscriber(b, client_id="dev", topic="state/#", clean=False)
    b.disconnect("dev", abnormal=False, now=1)
    result = b.connect("dev", clean_session=False, keepalive=60, now=2)
    assert result["session_present"] is True
    assert b.snapshot()["sessions"]["dev"]["subscriptions"]
    publisher(b)
    b.publish("pub", "state/online", b"1", qos=0, now=3)
    messages = b.poll("dev")
    assert len(messages) == 1
    assert messages[0]["payload"] == b"1"


def test_clean_session_true_wipes_previous_session():
    b = broker()
    subscriber(b, client_id="dev", topic="state/#", clean=False)
    b.disconnect("dev", abnormal=False, now=1)
    result = b.connect("dev", clean_session=True, keepalive=60, now=2)
    assert result["session_present"] is False
    assert b.snapshot()["sessions"]["dev"]["subscriptions"] == []
    publisher(b)
    b.publish("pub", "state/online", b"1", qos=0, now=3)
    assert b.poll("dev") == []


def test_empty_retained_payload_clears_retained_message():
    b = broker()
    publisher(b)
    b.publish("pub", "cfg/x", b"on", qos=0, retain=True, now=0)
    subscriber(b, client_id="s1", topic="cfg/#", qos=0)
    messages = b.poll("s1")
    assert len(messages) == 1
    assert messages[0]["payload"] == b"on"
    b.publish("pub", "cfg/x", b"", qos=0, retain=True, now=1)
    assert "cfg/x" not in b.snapshot()["retained"]
    subscriber(b, client_id="s2", topic="cfg/#", qos=0)
    assert b.poll("s2") == []


def test_will_is_sent_only_on_abnormal_disconnect():
    b = broker()
    subscriber(b, client_id="sub", topic="status/#", qos=0)
    will = {"topic": "status/dev", "payload": b"down", "qos": 0}
    b.connect("dev", clean_session=True, will=will, now=0)
    b.disconnect("dev", abnormal=False, now=1)
    assert b.poll("sub") == []
    b.connect("dev", clean_session=True, will=will, now=2)
    b.disconnect("dev", abnormal=True, now=3)
    messages = b.poll("sub")
    assert len(messages) == 1
    assert messages[0]["payload"] == b"down"


def test_normal_wildcard_does_not_match_system_topics():
    b = broker()
    subscriber(b, client_id="sub", topic="#", qos=0)
    publisher(b)
    b.publish("pub", "$SYS/uptime", b"9", qos=0, now=0)
    assert b.poll("sub") == []
    b.subscribe("sub", "$SYS/#", qos=0, now=1)
    b.publish("pub", "$SYS/load", b"7", qos=0, now=2)
    messages = b.poll("sub")
    assert len(messages) == 1
    assert messages[0]["topic"] == "$SYS/load"


def test_inflight_window_blocks_excess_deliveries():
    b = broker(inflight_limit=2)
    subscriber(b, client_id="sub", topic="w/#", qos=1)
    publisher(b)
    for index in range(1, 4):
        b.publish("pub", "w/%d" % index, str(index).encode(), qos=1,
                  packet_id=index, now=0)
    messages = b.poll("sub")
    assert len(messages) == 2
    session = b.snapshot()["sessions"]["sub"]
    assert session["inflight"] == 2
    assert session["queued"] == 1
    b.puback("sub", messages[0]["packet_id"], now=1)
    more = b.poll("sub")
    assert len(more) == 1
    assert more[0]["payload"] == b"3"


def test_packet_id_cannot_be_reused_before_ack():
    b = broker()
    publisher(b)
    b.publish("pub", "w/a", b"one", qos=1, packet_id=3, now=0)
    with pytest.raises(ValueError):
        b.publish("pub", "w/b", b"two", qos=1, packet_id=3, now=1)


def test_overlapping_subscriptions_deliver_once():
    b = broker()
    b.connect("sub", clean_session=True, now=0)
    b.subscribe("sub", "a/+", qos=1, now=0)
    b.subscribe("sub", "a/b", qos=0, now=0)
    publisher(b)
    b.publish("pub", "a/b", b"hit", qos=1, packet_id=1, now=0)
    messages = b.poll("sub")
    assert len(messages) == 1
    assert messages[0]["qos"] == 1
    assert b.snapshot()["delivered"] == 1


def test_keepalive_timeout_is_one_and_a_half():
    b = broker()
    b.connect("c", clean_session=True, keepalive=10, now=0)
    assert b.tick(15) == []
    assert b.snapshot()["sessions"]["c"]["connected"] is True
    assert b.tick(16) == ["c"]
    assert b.snapshot()["sessions"]["c"]["connected"] is False


def test_same_topic_keeps_publish_order_across_qos():
    b = broker(inflight_limit=1)
    subscriber(b, client_id="sub", topic="seq/#", qos=1)
    publisher(b)
    b.publish("pub", "seq/cmd", b"first", qos=1, packet_id=1, now=0)
    b.publish("pub", "seq/cmd", b"second", qos=0, now=0)
    first = b.poll("sub")
    assert len(first) == 1
    assert first[0]["payload"] == b"first"
    b.puback("sub", first[0]["packet_id"], now=1)
    second = b.poll("sub")
    assert len(second) == 1
    assert second[0]["payload"] == b"second"


def test_export_and_load_restore_will_after_restart():
    b1 = broker()
    subscriber(b1, client_id="sub", topic="wake/#", qos=0, clean=False)
    will = {"topic": "wake/dev", "payload": b"offline", "qos": 0}
    b1.connect("dev", clean_session=False, will=will, now=0)
    state = b1.export_state()
    b2 = broker()
    b2.load_state(state)
    b2.disconnect("dev", abnormal=True, now=10)
    messages = b2.poll("sub")
    assert len(messages) == 1
    assert messages[0]["payload"] == b"offline"


def test_topic_alias_is_isolated_per_connection():
    b = broker()
    b.set_topic_alias("c1", 1, "a/b")
    b.set_topic_alias("c2", 1, "c/d")
    assert b.resolve_alias("c1", 1) == "a/b"
    assert b.resolve_alias("c2", 1) == "c/d"
    with pytest.raises(ValueError):
        b.resolve_alias("c1", 2)
