import struct

import socks5_gateway as sg


class Connector:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def __call__(self, host, port):
        self.calls.append((host, port))
        return self.result


def gateway(users=None, connector=None, **kw):
    if connector is None:
        connector = Connector()
    return sg.Socks5Gateway(users=users, connector=connector, **kw), connector


def domain_request(host, port=80, cmd=1, rsv=0, atyp=3):
    raw = host.encode("ascii")
    return (
        bytes([0x05, cmd, rsv, atyp, len(raw)])
        + raw
        + struct.pack("!H", port)
    )


def ipv4_request(host, port=80, cmd=1, rsv=0):
    return (
        bytes([0x05, cmd, rsv, 0x01])
        + bytes(int(part) for part in host.split("."))
        + struct.pack("!H", port)
    )


def test_method_selection_uses_common_userpass():
    client, _ = gateway(users={"alice": "secret"})
    result = client.start(1, b"\x05\x02\x00\x02")
    assert result["complete"] is True
    assert result["reply"] == b"\x05\x02"
    assert client.state(1) == "auth"


def test_no_acceptable_method_closes_connection():
    client, _ = gateway(users={"alice": "secret"})
    result = client.start(1, b"\x05\x01\x00")
    assert result["complete"] is True
    assert result["reply"] == b"\x05\xff"
    assert client.state(1) == "closed"


def test_greeting_handles_split_reads():
    client, _ = gateway()
    first = client.start(1, b"\x05\x02")
    assert first["complete"] is False
    assert first["reply"] is None
    second = client.start(1, b"\x00\x00")
    assert second["complete"] is True
    assert second["reply"] == b"\x05\x00"
    assert client.state(1) == "ready"


def test_domain_name_is_passed_to_connector_unresolved():
    client, connector = gateway()
    client.start(1, b"\x05\x01\x00")
    result = client.request(1, domain_request("example.com", 80))
    assert result["code"] == 0
    assert result["target"]["host"] == "example.com"
    assert connector.calls == [("example.com", 80)]


def test_bracketed_ipv6_literal_is_parsed():
    client, connector = gateway()
    client.start(1, b"\x05\x01\x00")
    result = client.request(1, domain_request("[2001:db8::1]", 443))
    assert result["code"] == 0
    assert result["target"]["host"] == "2001:db8::1"
    assert connector.calls == [("2001:db8::1", 443)]


def test_zero_length_domain_is_rejected():
    client, connector = gateway()
    client.start(1, b"\x05\x01\x00")
    result = client.request(1, b"\x05\x01\x00\x03\x00" + struct.pack("!H", 80))
    assert result["code"] == 0x08
    assert result["state"] == "closed"
    assert connector.calls == []


def test_nonzero_reserved_byte_is_rejected():
    client, connector = gateway()
    client.start(1, b"\x05\x01\x00")
    result = client.request(1, ipv4_request("1.1.1.1", 80, rsv=1))
    assert result["code"] == 0x01
    assert result["state"] == "closed"
    assert connector.calls == []


def test_reply_waits_for_upstream_connect():
    failed, failed_connector = gateway(connector=Connector(result=False))
    failed.start(1, b"\x05\x01\x00")
    refused = failed.request(1, ipv4_request("1.1.1.1", 80))
    assert refused["code"] == 0x05
    assert failed.state(1) == "closed"
    assert failed_connector.calls == [("1.1.1.1", 80)]

    ok, ok_connector = gateway(connector=Connector(result=True))
    ok.start(1, b"\x05\x01\x00")
    success = ok.request(1, ipv4_request("1.1.1.1", 80))
    assert success["code"] == 0x00
    assert ok.state(1) == "connected"
    assert ok_connector.calls == [("1.1.1.1", 80)]


def test_bind_uses_separate_state_and_skips_connect():
    client, connector = gateway()
    client.start(1, b"\x05\x01\x00")
    result = client.request(1, ipv4_request("0.0.0.0", 0, cmd=2))
    assert result["code"] == 0x00
    assert result["state"] == "bind_pending"
    assert client.state(1) == "bind_pending"
    assert connector.calls == []


def test_udp_associate_validates_source():
    client, _ = gateway(udp_allow={"8.8.8.8"})
    client.start(1, b"\x05\x01\x00")
    associated = client.request(
        1, ipv4_request("0.0.0.0", 0, cmd=3), peer=("10.0.0.5", 40000)
    )
    assert associated["code"] == 0x00
    assert associated["state"] == "udp_associate"
    packet = {"host": "8.8.8.8", "port": 53, "payload": b"query"}
    bad = client.udp_send(1, packet, ("10.0.0.6", 40000))
    assert bad["accepted"] is False
    assert bad["reason"] == "bad_source"
    good = client.udp_send(1, packet, ("10.0.0.5", 40000))
    assert good["accepted"] is True


def test_udp_associate_restricts_target():
    client, _ = gateway(udp_allow={"8.8.8.8"})
    client.start(1, b"\x05\x01\x00")
    client.request(
        1, ipv4_request("0.0.0.0", 0, cmd=3), peer=("10.0.0.5", 40000)
    )
    denied = client.udp_send(
        1, {"host": "1.1.1.1", "port": 53, "payload": b"x"},
        ("10.0.0.5", 40000),
    )
    assert denied["accepted"] is False
    assert denied["reason"] == "target_denied"
    allowed = client.udp_send(
        1, {"host": "8.8.8.8", "port": 53, "payload": b"x"},
        ("10.0.0.5", 40000),
    )
    assert allowed["accepted"] is True


def test_auth_version_mismatch_is_permanent():
    client, _ = gateway(users={"alice": "secret"})
    client.start(1, b"\x05\x01\x02")
    result = client.authenticate(1, b"\x02\x05alice\x06secret")
    assert result["status"] == 1
    assert result["retryable"] is False
    assert client.state(1) == "closed"


def test_auth_wrong_password_is_retryable():
    client, _ = gateway(users={"alice": "secret"})
    client.start(1, b"\x05\x01\x02")
    wrong = client.authenticate(1, b"\x01\x05alice\x03bad")
    assert wrong["status"] == 1
    assert wrong["retryable"] is True
    good = client.authenticate(1, b"\x01\x05alice\x06secret")
    assert good["status"] == 0
    assert good["retryable"] is False
    assert client.state(1) == "ready"


def test_connections_have_independent_state():
    client, _ = gateway()
    client.start(1, b"\x05\x02")
    client.start(2, b"\x05\x01\x00")
    assert client.state(2) == "ready"
    assert client.state(1) == "greeting"
    finished = client.start(1, b"\x00\x00")
    assert finished["complete"] is True
    assert finished["reply"] == b"\x05\x00"


def test_forwarding_enforces_idle_and_byte_limits():
    client, _ = gateway(max_bytes=50, idle_timeout=30)
    client.start(1, b"\x05\x01\x00")
    client.request(1, ipv4_request("1.1.1.1", 80), now=0)
    allowed = client.forward(1, b"x" * 30, now=10)
    assert allowed["accepted"] is True
    assert allowed["bytes"] == 30
    blocked = client.forward(1, b"x" * 30, now=20)
    assert blocked["accepted"] is False
    assert blocked["reason"] == "byte_limit"

    idle, _ = gateway(max_bytes=1000, idle_timeout=30)
    idle.start(1, b"\x05\x01\x00")
    idle.request(1, ipv4_request("1.1.1.1", 80), now=0)
    timed_out = idle.forward(1, b"x", now=31)
    assert timed_out["accepted"] is False
    assert timed_out["reason"] == "idle_timeout"
