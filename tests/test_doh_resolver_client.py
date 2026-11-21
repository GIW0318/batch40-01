import doh_resolver_client as drc


DEFAULT_CERT = {"leaf": "leaf-1", "spki": "spki-ok"}


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request):
        self.calls.append(dict(request))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(rcode=0, answers=None, ttl=60, truncated=False,
             content_type="application/dns-message", cert=None,
             h2_error=None, status=200):
    return {
        "status": status,
        "content_type": content_type,
        "rcode": rcode,
        "answers": answers or [{"name": "example.com", "data": "192.0.2.1"}],
        "ttl": ttl,
        "truncated": truncated,
        "cert": dict(cert or DEFAULT_CERT),
        "h2_error": h2_error,
    }


def client(transport, **kw):
    return drc.DohResolverClient(transport=transport, **kw)


def test_query_uses_fixed_bootstrap_address():
    transport = Transport([response()])
    resolver = client(transport, bootstrap=("198.51.100.53",))
    result = resolver.resolve("example.com", now=0)
    assert result["ok"] is True
    assert transport.calls[0]["address"] == "198.51.100.53"
    assert transport.calls[0]["sni"] == "doh.example"


def test_message_id_is_always_zero():
    transport = Transport([response()])
    resolver = client(transport)
    resolver.resolve("example.com", now=0)
    assert transport.calls[0]["id"] == 0


def test_query_is_padded_to_block():
    transport = Transport([response()])
    resolver = client(transport, pad_block=128)
    resolver.resolve("example.com", now=0)
    request = transport.calls[0]
    assert request["padding"] > 0
    assert (request["unpadded_length"] + request["padding"]) % 128 == 0


def test_wrong_content_type_is_rejected():
    transport = Transport([response(content_type="text/html")])
    resolver = client(transport)
    result = resolver.resolve("example.com", now=0)
    assert result["ok"] is False
    assert result["error"] == "bad_content_type"


def test_certificate_pin_tracks_spki_not_leaf():
    transport = Transport([
        response(cert={"leaf": "leaf-1", "spki": "spki-ok"}),
        response(cert={"leaf": "leaf-2", "spki": "spki-ok"}),
    ])
    resolver = client(transport, pin_spki="spki-ok")
    assert resolver.resolve("example.com", now=0)["ok"] is True
    assert resolver.resolve("example.org", now=0)["ok"] is True


def test_certificate_pin_mismatch_is_rejected():
    transport = Transport([
        response(cert={"leaf": "leaf-1", "spki": "spki-bad"}),
    ])
    resolver = client(transport, pin_spki="spki-ok")
    result = resolver.resolve("example.com", now=0)
    assert result["ok"] is False
    assert result["error"] == "pin_mismatch"


def test_truncated_response_is_retried_over_tcp():
    transport = Transport([
        response(truncated=True),
        response(truncated=False),
    ])
    resolver = client(transport)
    result = resolver.resolve("example.com", now=0)
    assert result["ok"] is True
    assert len(transport.calls) == 2
    assert transport.calls[1]["tcp"] is True


def test_h2_refused_stream_retries_on_new_connection():
    transport = Transport([
        response(h2_error="REFUSED_STREAM"),
        response(),
    ])
    resolver = client(transport)
    result = resolver.resolve("example.com", now=0)
    assert result["ok"] is True
    assert len(transport.calls) == 2
    assert transport.calls[1]["connection"] != transport.calls[0]["connection"]


def test_cache_key_includes_dnssec_and_cd():
    transport = Transport([response(), response(), response()])
    resolver = client(transport)
    resolver.resolve("example.com", dnssec=True, now=0)
    resolver.resolve("example.com", dnssec=False, now=1)
    assert len(transport.calls) == 2
    resolver.resolve("example.com", dnssec=False, now=2)
    assert len(transport.calls) == 2
    resolver.resolve("example.com", dnssec=True, cd=True, now=3)
    assert len(transport.calls) == 3


def test_ttl_is_capped():
    transport = Transport([response(ttl=3600), response(ttl=3600)])
    resolver = client(transport, ttl_cap=300)
    result = resolver.resolve("example.com", now=0)
    assert result["ttl"] == 300
    resolver.resolve("example.com", now=299)
    assert len(transport.calls) == 1
    resolver.resolve("example.com", now=301)
    assert len(transport.calls) == 2


def test_response_codes_are_mapped_distinctly():
    transport = Transport([response(rcode=3), response(rcode=2)])
    resolver = client(transport)
    missing = resolver.resolve("missing.example", now=0)
    failure = resolver.resolve("failure.example", now=0)
    assert missing["rcode"] == 3
    assert missing["rcode_name"] == "NXDOMAIN"
    assert failure["rcode"] == 2
    assert failure["rcode_name"] == "SERVFAIL"
    assert missing["rcode_name"] != failure["rcode_name"]


def test_plaintext_fallback_is_opt_in_and_counted():
    transport = Transport([ConnectionError("doh down"), response()])
    resolver = client(transport, allow_plaintext=False)
    failed = resolver.resolve("example.com", now=0)
    assert failed["ok"] is False
    assert failed["error"] == "doh_failed"
    assert len(transport.calls) == 1
    assert resolver.stats()["fallback"] == 0

    fallback_transport = Transport([ConnectionError("doh down"), response()])
    fallback = client(fallback_transport, allow_plaintext=True)
    result = fallback.resolve("example.com", now=0)
    assert result["ok"] is True
    assert result["transport"] == "udp"
    assert len(fallback_transport.calls) == 2
    assert fallback_transport.calls[1]["transport"] == "udp"
    assert fallback.stats()["fallback"] == 1


def test_each_query_gets_its_own_stream():
    transport = Transport([response(), response()])
    resolver = client(transport)
    resolver.resolve("a.example", now=0)
    resolver.resolve("b.example", now=0)
    streams = {call["stream_id"] for call in transport.calls}
    assert len(streams) == 2


def test_bootstrap_cache_expires():
    calls = []

    def provider():
        calls.append(1)
        return ["203.0.113.9"]

    transport = Transport([response()])
    resolver = client(
        transport,
        bootstrap=("198.51.100.53",),
        bootstrap_ttl=100,
        bootstrap_resolver=provider,
    )
    assert resolver.bootstrap_address(0) == "198.51.100.53"
    assert resolver.bootstrap_address(50) == "198.51.100.53"
    assert calls == []
    assert resolver.bootstrap_address(150) == "203.0.113.9"
    assert calls == [1]
    assert resolver.stats()["bootstrap_refreshes"] == 1


def test_stats_report_queries_and_cache_hits():
    transport = Transport([response(), response()])
    resolver = client(transport)
    resolver.resolve("example.com", now=0)
    resolver.resolve("example.com", now=1)
    stats = resolver.stats()
    assert stats["queries"] == 2
    assert stats["cache_hits"] == 1
    assert stats["errors"] == 0
