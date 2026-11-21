import pytest

import sni_cert_router as scr


def cert(domain, cert_id, not_before=0, not_after=1000,
         alpn=None, ocsp=b"", default=False):
    return {
        "domain": domain,
        "cert_id": cert_id,
        "not_before": not_before,
        "not_after": not_after,
        "alpn": list(alpn or ["h2", "http/1.1"]),
        "ocsp": ocsp,
        "default": default,
    }


def router(default_domain=None, certs=None, now=0):
    instance = scr.SniCertRouter(default_domain=default_domain)
    instance.load_batch(certs or [
        cert("example.com", "base"),
        cert("a.example.com", "exact"),
        cert("*.example.com", "wild"),
    ])
    return instance


def test_normalize_lowercases_and_strips_trailing_dot():
    instance = scr.SniCertRouter()
    assert instance.normalize("Example.COM.") == "example.com"


def test_normalize_unicode_domain_to_alabel():
    instance = scr.SniCertRouter()
    assert instance.normalize("例子.example") == "xn--fsqu00a.example"


def test_exact_match_beats_wildcard():
    selected = router().select("a.example.com", now=10)
    assert selected["cert_id"] == "exact"


def test_wildcard_matches_exactly_one_label():
    selected = router().select("b.example.com", now=10)
    assert selected["cert_id"] == "wild"


def test_wildcard_does_not_cross_multiple_labels():
    with pytest.raises(ValueError):
        router().select("b.a.example.com", now=10)


def test_missing_sni_uses_default_or_is_rejected():
    with_default = router(default_domain="example.com")
    assert with_default.select(None, now=10)["cert_id"] == "base"
    with pytest.raises(ValueError):
        router().select(None, now=10)


def test_alpn_uses_intersection_and_rejects_empty_overlap():
    instance = router()
    assert instance.select("example.com", alpn=["http/1.1"], now=10)["alpn"] == "http/1.1"
    assert instance.select("example.com", alpn=["http/1.1", "h2"], now=10)["alpn"] == "h2"
    with pytest.raises(ValueError):
        instance.select("example.com", alpn=["spdy"], now=10)


def test_session_ticket_is_bound_to_domain():
    instance = router()
    ticket = instance.issue_ticket("example.com", 0)["ticket"]
    assert instance.resume_ticket(ticket, "example.com", 10) is True
    assert instance.resume_ticket(ticket, "a.example.com", 10) is False


def test_session_ticket_expires():
    instance = router()
    ticket = instance.issue_ticket("example.com", 0)["ticket"]
    assert instance.resume_ticket(ticket, "example.com", 299) is True
    assert instance.resume_ticket(ticket, "example.com", 301) is False


def test_duplicate_certificate_domains_are_rejected():
    instance = scr.SniCertRouter()
    with pytest.raises(ValueError):
        instance.load_batch([
            cert("Example.com.", "one"),
            cert("example.COM", "two"),
        ])


def test_invalid_validity_window_and_expiry_are_rejected():
    instance = scr.SniCertRouter()
    with pytest.raises(ValueError):
        instance.load_batch([cert("example.com", "bad", not_before=100, not_after=50)])
    instance.load_batch([cert("example.com", "short", not_before=10, not_after=20)])
    with pytest.raises(ValueError):
        instance.select("example.com", now=5)
    with pytest.raises(ValueError):
        instance.select("example.com", now=21)


def test_hot_reload_switches_batch_atomically_and_updates_staple():
    instance = router()
    assert instance.select("example.com", now=10)["ocsp"] == b""
    version = instance.load_batch([
        cert("example.com", "new-base", ocsp=b"fresh"),
        cert("*.example.com", "new-wild", ocsp=b"fresh"),
    ])
    assert version == 2
    selected = instance.select("example.com", now=10)
    assert selected["cert_id"] == "new-base"
    assert selected["ocsp"] == b"fresh"
    assert selected["version"] == 2
    assert instance.select("a.example.com", now=10)["cert_id"] == "new-wild"


def test_illegal_sni_is_rejected():
    instance = router()
    with pytest.raises(ValueError):
        instance.select("", now=10)
    with pytest.raises(ValueError):
        instance.select("bad\x00.example.com", now=10)
    with pytest.raises(ValueError):
        instance.select("\udcff.example.com", now=10)


def test_wildcard_is_only_allowed_as_leftmost_label():
    instance = scr.SniCertRouter()
    with pytest.raises(ValueError):
        instance.load_batch([cert("a.*.example.com", "bad")])
    with pytest.raises(ValueError):
        instance.load_batch([cert("*.*.example.com", "worse")])


def test_equivalent_domain_spellings_route_to_same_cert():
    instance = scr.SniCertRouter()
    instance.load_batch([
        cert("xn--fsqu00a.example", "idn"),
        cert("Mixed.Example.", "mixed"),
    ])
    assert instance.select("例子.example", now=10)["cert_id"] == "idn"
    assert instance.select("mixed.example", now=10)["cert_id"] == "mixed"
    assert instance.select("MIXED.EXAMPLE.", now=10)["cert_id"] == "mixed"
