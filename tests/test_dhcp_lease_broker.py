import json

import dhcp_lease_broker as dlb


POOL = [{"subnet": "10.0.0.0/24", "start": "10.0.0.10", "end": "10.0.0.12"}]


def request(mac="AA-BB-CC-DD-EE-01", ip=None, lease=3600, broadcast=False,
            client_id=None, relay=None, overload=None):
    return {
        "mac": mac,
        "requested_ip": ip,
        "lease_time": lease,
        "broadcast": broadcast,
        "client_id": client_id,
        "relay": relay or {},
        "overload": overload or {},
    }


def broker(pools=None, reservations=None, **kw):
    return dlb.DhcpLeaseBroker(
        pools=pools if pools is not None else POOL,
        reservations=reservations,
        **kw,
    )


def test_normalize_mac_formats():
    table = broker()
    assert table.normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert table.normalize_mac("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"
    assert table.normalize_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"


def test_reservation_matches_normalized_mac():
    table = broker(
        reservations={"aa:bb:cc:dd:ee:ff": "10.0.0.11"}
    )
    result = table.request(request(mac="AA-BB-CC-DD-EE-FF"), now=0)
    assert result["type"] == "ack"
    assert result["ip"] == "10.0.0.11"


def test_same_client_mac_and_client_id_share_lease():
    table = broker()
    first = table.request(
        request(mac="aa:bb:cc:dd:ee:01", client_id="cid-1"), now=0
    )
    second = table.request(
        request(mac="AA-BB-CC-DD-EE-01", client_id=None), now=1
    )
    assert first["ip"] == second["ip"]


def test_expired_leases_are_reclaimed_for_new_clients():
    table = broker(
        pools=[{"subnet": "10.0.0.0/24", "start": "10.0.0.10",
                "end": "10.0.0.11"}]
    )
    assert table.request(request(mac="aa:bb:cc:dd:ee:01", lease=60),
                         now=0)["ip"] == "10.0.0.10"
    assert table.request(request(mac="aa:bb:cc:dd:ee:02", lease=60),
                         now=0)["ip"] == "10.0.0.11"
    blocked = table.request(request(mac="aa:bb:cc:dd:ee:03"), now=0)
    assert blocked["type"] == "nak"
    assert blocked["reason"] == "pool_exhausted"
    assert table.expire(now=61) == 2
    reclaimed = table.request(request(mac="aa:bb:cc:dd:ee:03"), now=61)
    assert reclaimed["type"] == "ack"
    assert reclaimed["ip"] == "10.0.0.10"


def test_pool_exhaustion_returns_nak():
    table = broker(
        pools=[{"subnet": "10.0.0.0/24", "start": "10.0.0.10",
                "end": "10.0.0.10"}]
    )
    assert table.request(request(mac="aa:bb:cc:dd:ee:01"), now=0)["type"] == "ack"
    result = table.request(request(mac="aa:bb:cc:dd:ee:02"), now=0)
    assert result["type"] == "nak"
    assert result["reason"] == "pool_exhausted"


def test_offer_and_request_are_atomic():
    table = broker()
    first = table.offer(
        request(mac="aa:bb:cc:dd:ee:01", ip="10.0.0.10"), now=0
    )
    second = table.offer(
        request(mac="aa:bb:cc:dd:ee:02", ip="10.0.0.10"), now=0
    )
    assert first["ip"] == "10.0.0.10"
    assert second["ip"] != "10.0.0.10"
    conflict = table.request(
        request(mac="aa:bb:cc:dd:ee:02", ip="10.0.0.10"), now=1
    )
    assert conflict["type"] == "nak"
    assert conflict["reason"] == "reservation_conflict"
    confirmed = table.request(
        request(mac="aa:bb:cc:dd:ee:01", ip="10.0.0.10"), now=1
    )
    assert confirmed["type"] == "ack"
    assert confirmed["ip"] == "10.0.0.10"


def test_option82_is_echoed_and_selects_relay_subnet():
    pools = [
        {"subnet": "10.0.0.0/24", "start": "10.0.0.10", "end": "10.0.0.12"},
        {"subnet": "10.1.0.0/24", "start": "10.1.0.10", "end": "10.1.0.12"},
    ]
    table = broker(pools=pools)
    result = table.request(
        request(relay={"giaddr": "10.1.0.1", "option82": "circuit-7"}),
        now=0,
    )
    assert result["subnet"] == "10.1.0.0/24"
    assert result["relay_option82"] == "circuit-7"
    assert result["ip"].startswith("10.1.0.")


def test_infinite_lease_is_clamped_to_max():
    table = broker()
    result = table.request(request(lease=4294967295), now=0)
    assert result["lease_time"] == 86400
    short = table.request(
        request(mac="aa:bb:cc:dd:ee:02", lease=1), now=0
    )
    assert short["lease_time"] == 60


def test_declined_address_is_quarantined():
    table = broker()
    first = table.request(request(mac="aa:bb:cc:dd:ee:01"), now=0)
    assert first["ip"] == "10.0.0.10"
    table.decline(
        request(mac="aa:bb:cc:dd:ee:01", ip="10.0.0.10"), now=1
    )
    second = table.request(request(mac="aa:bb:cc:dd:ee:02"), now=2)
    assert second["ip"] != "10.0.0.10"


def test_quarantine_expires():
    table = broker()
    table.request(request(mac="aa:bb:cc:dd:ee:01"), now=0)
    table.decline(
        request(mac="aa:bb:cc:dd:ee:01", ip="10.0.0.10"), now=1
    )
    assert table.request(request(mac="aa:bb:cc:dd:ee:02"),
                         now=2)["ip"] == "10.0.0.11"
    assert table.request(request(mac="aa:bb:cc:dd:ee:03"),
                         now=2)["ip"] == "10.0.0.12"
    blocked = table.request(request(mac="aa:bb:cc:dd:ee:04"), now=2)
    assert blocked["type"] == "nak"
    table.expire(now=301)
    reclaimed = table.request(request(mac="aa:bb:cc:dd:ee:04"), now=301)
    assert reclaimed["type"] == "ack"
    assert reclaimed["ip"] == "10.0.0.10"


def test_option_overload_is_parsed():
    table = broker()
    result = table.request(
        request(
            overload={
                "file": {"hostname": "printer"},
                "sname": {"domain": "corp"},
            }
        ),
        now=0,
    )
    assert result["parsed_options"]["hostname"] == "printer"
    assert result["parsed_options"]["domain"] == "corp"


def test_static_reservation_is_excluded_from_dynamic_pool():
    table = broker(
        reservations={"aa:bb:cc:dd:ee:ff": "10.0.0.10"}
    )
    dynamic = table.request(request(mac="aa:bb:cc:dd:ee:01"), now=0)
    assert dynamic["ip"] == "10.0.0.11"
    reserved = table.request(request(mac="AA-BB-CC-DD-EE-FF"), now=1)
    assert reserved["ip"] == "10.0.0.10"


def test_renewal_extends_from_original_expiry():
    table = broker()
    first = table.request(request(mac="aa:bb:cc:dd:ee:01", lease=3600),
                          now=0)
    assert first["expires_at"] == 3600
    renewed = table.request(
        request(mac="aa:bb:cc:dd:ee:01", lease=3600), now=1000
    )
    assert renewed["expires_at"] == 7200


def test_broadcast_flag_controls_reply_mode():
    table = broker()
    broadcast = table.request(
        request(mac="aa:bb:cc:dd:ee:01", broadcast=True), now=0
    )
    unicast = table.request(
        request(mac="aa:bb:cc:dd:ee:02", broadcast=False), now=1
    )
    assert broadcast["reply"] == "broadcast"
    assert unicast["reply"] == "unicast"


def test_state_persists_across_restart():
    table = broker()
    table.request(request(mac="aa:bb:cc:dd:ee:01"), now=0)
    state = table.dump_state()
    encoded = json.dumps(state)
    restored = broker()
    restored.load_state(json.loads(encoded))
    assert restored.leases()["10.0.0.10"]["mac"] == "aa:bb:cc:dd:ee:01"
    other = restored.request(request(mac="aa:bb:cc:dd:ee:02"), now=1)
    assert other["ip"] != "10.0.0.10"
