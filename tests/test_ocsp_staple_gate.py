import ocsp_staple_gate as osg


def cert(subject, issuer, serial, must_staple=False):
    return {
        "subject": subject,
        "issuer": issuer,
        "serial": serial,
        "issuer_key_hash": "h-" + issuer,
        "must_staple": must_staple,
    }


CHAIN = [
    cert("leaf", "intermediate", "0A1B"),
    cert("intermediate", "root", "0A1C"),
]


def staple(serial="0A1B", issuer="intermediate", status="good", this=100,
           next=200, nonce="n-1", signer="intermediate", authorized=True,
           size=512):
    return {
        "serial": serial,
        "issuer_key_hash": "h-" + issuer,
        "status": status,
        "this_update": this,
        "next_update": next,
        "nonce": nonce,
        "signer": signer,
        "authorized": authorized,
        "size": size,
        "body": b"staple",
    }


def test_must_staple_leaf_requires_staple():
    chain = [
        cert("leaf", "intermediate", "0A1B", must_staple=True),
        cert("intermediate", "root", "0A1C"),
    ]
    result = osg.StapleGate().verify(chain, [], now=150)
    assert result["ok"] is False
    assert result["reason"] == "must_staple_missing"
    assert result["soft_failed"] is False


def test_non_must_staple_leaf_without_staple_is_ok():
    result = osg.StapleGate().verify(CHAIN, [], now=150)
    assert result["ok"] is True


def test_future_this_update_is_rejected():
    gate = osg.StapleGate(clock_skew=10)
    result = gate.verify(CHAIN, [staple(this=200, next=300)], now=150)
    assert result["ok"] is False
    assert result["reason"] == "not_yet_valid"


def test_expired_next_update_is_rejected():
    gate = osg.StapleGate(clock_skew=10)
    result = gate.verify(CHAIN, [staple(this=50, next=100)], now=150)
    assert result["ok"] is False
    assert result["reason"] == "expired"


def test_validity_boundaries_respect_clock_skew():
    gate = osg.StapleGate(clock_skew=30)
    fresh = gate.verify(CHAIN, [staple(this=100, next=200)], now=70)
    assert fresh["ok"] is True
    stale = gate.verify(CHAIN, [staple(this=100, next=200)], now=230)
    assert stale["ok"] is True


def test_unauthorized_signer_is_rejected():
    result = osg.StapleGate().verify(
        CHAIN,
        [staple(signer="rogue", authorized=False)],
        now=150,
    )
    assert result["ok"] is False
    assert result["reason"] == "unauthorized_signer"


def test_authorized_delegated_signer_is_accepted():
    result = osg.StapleGate().verify(
        CHAIN,
        [staple(signer="delegated", authorized=True)],
        now=150,
    )
    assert result["ok"] is True


def test_nonce_mismatch_is_rejected():
    gate = osg.StapleGate()
    bad = gate.verify(CHAIN, [staple(nonce="n-1")], nonce="n-2", now=150)
    assert bad["ok"] is False
    assert bad["reason"] == "nonce_mismatch"
    good = gate.verify(CHAIN, [staple(nonce="n-1")], nonce="n-1", now=150)
    assert good["ok"] is True


def test_revoked_status_fails_immediately():
    result = osg.StapleGate().verify(
        CHAIN, [staple(status="revoked")], now=150
    )
    assert result["ok"] is False
    assert result["reason"] == "revoked"
    assert result["soft_failed"] is False


def test_binding_fields_must_match_leaf():
    gate = osg.StapleGate()
    serial_mismatch = gate.verify(CHAIN, [staple(serial="FFFF")], now=150)
    assert serial_mismatch["ok"] is False
    assert serial_mismatch["reason"] == "binding_mismatch"
    issuer_mismatch = gate.verify(
        CHAIN, [staple(issuer="other")], now=150
    )
    assert issuer_mismatch["ok"] is False
    assert issuer_mismatch["reason"] == "binding_mismatch"


def test_only_network_errors_can_soft_fail():
    chain = [
        cert("leaf", "intermediate", "0A1B", must_staple=True),
        cert("intermediate", "root", "0A1C"),
    ]
    gate = osg.StapleGate(allow_soft_fail=True)
    soft = gate.verify(chain, [], now=150, fetch_error="timeout")
    assert soft["ok"] is True
    assert soft["soft_failed"] is True
    assert soft["reason"] == "soft_fail_network"
    malformed = gate.verify(
        chain, [staple(status="bogus")], now=150
    )
    assert malformed["ok"] is False
    assert malformed["soft_failed"] is False
    assert malformed["reason"] == "invalid_status"


def test_cache_key_includes_full_chain():
    gate = osg.StapleGate()
    other_chain = [
        CHAIN[0],
        cert("intermediate-2", "root", "0A1D"),
    ]
    assert gate.cache_key(CHAIN) != gate.cache_key(other_chain)
    gate.store(CHAIN, staple(), version=1)
    assert gate.cache(CHAIN)["serial"] == "0A1B"
    assert gate.cache(other_chain) is None


def test_newer_fetch_is_not_overwritten_by_older():
    responses = [
        {"staple": staple(nonce="new"), "version": 2},
        {"staple": staple(nonce="old"), "version": 1},
    ]
    gate = osg.StapleGate(fetch_source=lambda chain: responses.pop(0))
    gate.fetch(CHAIN, now=0)
    gate.fetch(CHAIN, now=1)
    assert gate.cache(CHAIN)["nonce"] == "new"


def test_oversized_staple_is_rejected():
    result = osg.StapleGate(max_staple_size=1024).verify(
        CHAIN, [staple(size=20000)], now=150
    )
    assert result["ok"] is False
    assert result["reason"] == "staple_too_large"


def test_every_certificate_in_chain_is_checked():
    chain = [
        cert("leaf", "intermediate", "0A1B", must_staple=True),
        cert("intermediate", "root", "0A1C", must_staple=True),
    ]
    gate = osg.StapleGate()
    missing = gate.verify(chain, [staple()], now=150)
    assert missing["ok"] is False
    assert missing["reason"] == "must_staple_missing"
    assert missing["cert_results"][0]["ok"] is True
    assert missing["cert_results"][1]["ok"] is False
    both = [
        staple(),
        staple(
            serial="0A1C",
            issuer="root",
            signer="root",
            status="revoked",
        ),
    ]
    revoked = gate.verify(chain, both, now=150)
    assert revoked["ok"] is False
    assert revoked["reason"] == "revoked"
    assert revoked["cert_results"][1]["ok"] is False
