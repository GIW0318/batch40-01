import pytest

import dnssec_negative_trust as dnt

ZONE = "example."


def validator(tags=(1,), limit=100):
    guard = dnt.NegativeTrustAnchor(nsec3_iterations_limit=limit)
    guard.add_anchor(ZONE, tags)
    return guard


def rr(name="www.example.", valid=True, tag=1):
    return {"name": name, "type": "A", "signature": {"key_tag": tag, "valid": valid}}


def packet(*rrsets, signed=True, dnskey=(1,), cd=False):
    return {"signed": signed, "dnskey": list(dnskey), "rrsets": list(rrsets), "cd": cd}


def test_signed_zone_without_dnskey_is_bogus_and_unsigned_is_insecure():
    guard = validator()
    unsigned = guard.validate(ZONE, packet(rr(), signed=False), 0)
    missing = guard.validate(ZONE, packet(rr(), dnskey=()), 0)
    assert unsigned["status"] == "insecure"
    assert missing["status"] == "bogus"
    assert missing["reason"] == "missing_dnskey"


def test_valid_signature_is_secure():
    result = validator().validate(ZONE, packet(rr()), 0)
    assert result["status"] == "secure"
    assert result["ad"] is True
    assert result["cd"] is False


def test_invalid_signature_is_bogus():
    result = validator().validate(ZONE, packet(rr(valid=False)), 0)
    assert result["status"] == "bogus"
    assert result["reason"] == "bad_signature"


def test_cname_chain_validates_every_hop():
    guard = validator()
    bad = guard.validate(ZONE, packet(
        rr("a.example.", valid=True),
        rr("b.example.", valid=False),
    ), 0)
    good = guard.validate(ZONE, packet(
        rr("a.example.", valid=True),
        rr("b.example.", valid=True),
    ), 0)
    assert bad["status"] == "bogus"
    assert good["status"] == "secure"


def test_rotation_accepts_old_and_new_anchors():
    guard = validator({1})
    guard.rotate_anchor(ZONE, {2})
    assert guard.trust_anchor(ZONE) == [1, 2]
    assert guard.validate(ZONE, packet(rr(tag=1), dnskey=(1, 2)), 0)["status"] == "secure"
    assert guard.validate(ZONE, packet(rr(tag=2), dnskey=(1, 2)), 0)["status"] == "secure"


def test_finalized_rotation_rejects_old_anchor():
    guard = validator({1})
    guard.rotate_anchor(ZONE, {2})
    guard.finalize_rotation(ZONE)
    old = guard.validate(ZONE, packet(rr(tag=1)), 0)
    new = guard.validate(ZONE, packet(rr(tag=2), dnskey=(2,)), 0)
    assert old["status"] == "bogus"
    assert old["reason"] == "untrusted_key"
    assert new["status"] == "secure"


def test_nsec3_iterations_over_limit_is_bogus():
    response = {
        "name": "b.example.",
        "type": "NXDOMAIN",
        "nsec3": [{"iterations": 150, "ttl": 60, "opt_out": False, "covers": ["b.example."]}],
    }
    result = validator(limit=100).negative_proof(response, 0)
    assert result["status"] == "bogus"
    assert result["reason"] == "nsec3_iterations"


def test_nsec_range_boundary_excludes_the_next_name():
    guard = validator()
    base = {"type": "NXDOMAIN", "nsec": [{"owner": "a.example.", "next": "c.example.", "ttl": 300}]}
    inside = dict(base, name="b.example.")
    boundary = dict(base, name="c.example.")
    assert guard.negative_proof(inside, 0)["status"] == "secure"
    assert guard.negative_proof(boundary, 0)["status"] == "bogus"


def test_opt_out_proof_is_insecure_not_secure():
    response = {
        "name": "b.example.",
        "type": "NXDOMAIN",
        "nsec3": [{"iterations": 5, "ttl": 45, "opt_out": True, "covers": ["b.example."]}],
    }
    result = validator().negative_proof(response, 0)
    assert result["status"] == "insecure"
    assert result["reason"] == "opt_out"
    assert result["ttl"] == 45


def test_wildcard_proof_needs_closest_encloser():
    guard = validator()
    valid = {
        "name": "a.example.",
        "type": "NODATA",
        "wildcard": True,
        "closest_encloser": "example.",
        "next_closer": "a.example.",
        "nsec3": [{"iterations": 0, "ttl": 30, "opt_out": False, "covers": ["a.example."]}],
    }
    broken = dict(valid, closest_encloser=None)
    assert guard.negative_proof(valid, 0)["status"] == "secure"
    rejected = guard.negative_proof(broken, 0)
    assert rejected["status"] == "bogus"
    assert rejected["reason"] == "wildcard_proof_incomplete"


def test_negative_ttl_uses_proof_minimum_not_soa():
    response = {
        "name": "b.example.",
        "type": "NXDOMAIN",
        "nsec": [{"owner": "a.example.", "next": "c.example.", "ttl": 300}],
        "nsec3": [{"iterations": 0, "ttl": 60, "opt_out": False, "covers": ["b.example."]}],
        "soa": {"minimum": 30},
    }
    result = validator().negative_proof(response, 0)
    assert result["status"] == "secure"
    assert result["ttl"] == 60


def test_bogus_result_is_not_stored_in_normal_cache():
    guard = validator()
    assert guard.store("b.example.", "bogus", 60, 0) is False
    assert guard.lookup("b.example.", 1) is None
    assert guard.store("c.example.", "secure", 60, 0) is True
    assert guard.lookup("c.example.", 1)["status"] == "secure"


def test_cache_lookup_respects_expiry():
    guard = validator()
    guard.store("b.example.", "secure", 60, 0)
    assert guard.lookup("b.example.", 59) is not None
    assert guard.lookup("b.example.", 61) is None


def test_cd_flag_is_preserved_and_ad_cleared():
    guard = validator()
    result = guard.validate(ZONE, packet(rr()), 0, cd=True)
    assert result["status"] == "insecure"
    assert result["cd"] is True
    assert result["ad"] is False


def test_truncated_udp_response_retries_over_tcp():
    guard = validator()
    truncated = guard.resolve("www.example.", {"truncated": True, "transport": "udp", "zone": ZONE}, 0)
    normal_packet = dict(packet(rr(), signed=True, dnskey=(1,)), zone=ZONE)
    normal = guard.resolve("www.example.", normal_packet, 0)
    assert truncated["transport"] == "tcp"
    assert normal["transport"] == "udp"
    assert normal["status"] == "secure"
