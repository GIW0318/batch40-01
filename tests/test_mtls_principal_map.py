import pytest

import mtls_principal_map as mpm


def make_cert(fingerprint, subject, issuer, cn="ops", uris=None, dns=None,
              critical=None, not_before=0, not_after=100, self_signed=False):
    return {
        "fingerprint": fingerprint,
        "subject": subject,
        "issuer": issuer,
        "cn": cn,
        "san_uri": list(uris or []),
        "san_dns": list(dns or []),
        "critical_extensions": list(critical or []),
        "not_before": not_before,
        "not_after": not_after,
        "self_signed": self_signed,
    }


def root_cert(fingerprint="fp-root", subject="CN=root,O=corp", not_after=1000):
    return make_cert(fingerprint, subject, subject, cn="root",
                     not_after=not_after, self_signed=True)


def chain_for(uri="spiffe://corp.example/dept-a/ops", fingerprint="fp-leaf",
              leaf_extra=None):
    leaf = make_cert(fingerprint, "CN=ops,OU=dept-a,O=corp",
                     "CN=inter,O=corp", uris=[uri] if uri else None,
                     **(leaf_extra or {}))
    inter = make_cert("fp-inter", "CN=inter,O=corp", "CN=root,O=corp")
    return [leaf, inter]


def mapper(**kwargs):
    instance = mpm.MtlsPrincipalMap(**kwargs)
    instance.add_root(root_cert())
    return instance


def test_uri_san_is_preferred_over_common_name():
    m = mapper()
    first = m.map_request({"peer_chain": chain_for("spiffe://corp.example/dept-a/ops", "fp-a")})
    second = m.map_request({"peer_chain": chain_for("spiffe://corp.example/dept-b/ops", "fp-b")})
    assert first["principal"] == "spiffe://corp.example/dept-a/ops"
    assert second["principal"] == "spiffe://corp.example/dept-b/ops"
    assert first["principal"] != second["principal"]


def test_percent_encoded_unreserved_uri_is_canonicalized():
    m = mapper()
    encoded = m.map_request({"peer_chain": chain_for("spiffe://corp.example/dept-a/op%73", "fp-enc")})
    plain = m.map_request({"peer_chain": chain_for("spiffe://corp.example/dept-a/ops", "fp-plain")})
    assert encoded["principal"] == "spiffe://corp.example/dept-a/ops"
    assert encoded["principal"] == plain["principal"]


def test_encoded_slash_stays_a_distinct_principal():
    m = mapper()
    encoded = m.map_request({"peer_chain": chain_for("spiffe://corp.example/dept-a%2Fops", "fp-slash")})
    plain = m.map_request({"peer_chain": chain_for("spiffe://corp.example/dept-a/ops", "fp-noslash")})
    assert encoded["principal"] != plain["principal"]
    assert "%2F" in encoded["principal"]


def test_wildcard_matches_only_one_leftmost_label():
    m = mapper()
    assert m.match_dns("*.corp.example", "a.corp.example") is True
    assert m.match_dns("*.corp.example", "a.b.corp.example") is False
    assert m.match_dns("*.corp.example", "corp.example") is False
    assert m.match_dns("*.*.corp.example", "a.b.corp.example") is False
    wildcard = make_cert("fp-wild", "CN=svc", "CN=inter,O=corp", dns=["*.corp.example"])
    assert m.authorize_host(wildcard, "a.corp.example") is True
    assert m.authorize_host(wildcard, "a.b.corp.example") is False


def test_untrusted_self_signed_intermediate_is_rejected():
    m = mapper()
    rogue = make_cert("fp-rogue", "CN=rogue,O=corp", "CN=rogue,O=corp",
                      self_signed=True)
    leaf = make_cert("fp-leaf2", "CN=ops,OU=dept-a,O=corp", "CN=rogue,O=corp",
                     uris=["spiffe://corp.example/dept-a/ops"])
    with pytest.raises(ValueError):
        m.map_request({"peer_chain": [leaf, rogue]})


def test_chain_is_completed_with_trusted_root():
    m = mapper()
    result = m.map_request({"peer_chain": chain_for()})
    assert result["chain"] == ["fp-leaf", "fp-inter", "fp-root"]
    assert result["fingerprint"] == "fp-leaf"


def test_unknown_critical_extension_is_rejected():
    m = mapper()
    with pytest.raises(ValueError):
        m.map_request({"peer_chain": chain_for(leaf_extra={"critical": ["1.2.3.4.5"]})})
    known = mapper(known_critical_extensions=["1.2.3.4.5"])
    result = known.map_request({
        "peer_chain": chain_for(leaf_extra={"critical": ["1.2.3.4.5"]})
    })
    assert result["principal"] == "spiffe://corp.example/dept-a/ops"


def test_expired_and_not_yet_valid_certificates_are_rejected():
    m = mapper()
    with pytest.raises(ValueError):
        m.map_request({"peer_chain": chain_for()}, now=100)
    with pytest.raises(ValueError):
        m.map_request({
            "peer_chain": chain_for(leaf_extra={"not_before": 50})
        }, now=40)


def test_cached_mapping_expires_with_certificate():
    m = mapper(cache_ttl=60)
    mapping = m.map_request({"peer_chain": chain_for()}, now=40)
    assert mapping["expires_at"] == 100
    assert m.revalidate(mapping, 99)["status"] == "ok"
    assert m.revalidate(mapping, 100)["status"] == "expired"
    with pytest.raises(ValueError):
        m.map_request({"peer_chain": chain_for()}, now=101)


def test_revocation_is_rechecked_after_connection_interval():
    m = mapper(revocation_check_interval=300)
    mapping = m.map_request({
        "peer_chain": chain_for(leaf_extra={"not_after": 1000})
    }, now=0)
    m.revoke("fp-leaf", at=10)
    assert m.revalidate(mapping, 100)["status"] == "ok"
    assert m.revalidate(mapping, 400)["status"] == "revoked"


def test_new_connection_sees_revocation_immediately():
    m = mapper()
    m.map_request({"peer_chain": chain_for()}, now=0)
    m.revoke("fp-leaf", at=10)
    with pytest.raises(ValueError):
        m.map_request({"peer_chain": chain_for()}, now=20)


def test_root_rotation_trusts_old_and_new_roots():
    m = mapper()
    new_root = root_cert("fp-root-b", "CN=root-b,O=corp")
    m.add_trust_bundle("rotation-b", [new_root])
    leaf = make_cert("fp-new", "CN=ops,OU=dept-b,O=corp", "CN=root-b,O=corp",
                     uris=["spiffe://corp.example/dept-b/ops"])
    result = m.map_request({"peer_chain": [leaf]})
    assert result["principal"] == "spiffe://corp.example/dept-b/ops"
    assert result["chain"] == ["fp-new", "fp-root-b"]
    old = m.map_request({"peer_chain": chain_for()})
    assert old["principal"] == "spiffe://corp.example/dept-a/ops"


def test_cn_fallback_is_opt_in():
    no_san = {"uri": None, "leaf_extra": {"cn": "Ops"}}
    with pytest.raises(ValueError):
        mapper().map_request({"peer_chain": chain_for(**no_san)})
    fallback = mapper(allow_cn_fallback=True)
    result = fallback.map_request({"peer_chain": chain_for(**no_san)})
    assert result["principal"] == "cn:ops"


def test_case_and_trailing_dot_are_normalized():
    m = mapper(allow_cn_fallback=True)
    dotted = m.map_request({
        "peer_chain": chain_for(uri=None, fingerprint="fp-dot",
                                leaf_extra={"cn": "Ops.", "dns": ["OPS.CORP.EXAMPLE."]})
    })
    plain = m.map_request({
        "peer_chain": chain_for(uri=None, fingerprint="fp-plain-cn",
                                leaf_extra={"cn": "ops", "dns": ["ops.corp.example"]})
    })
    assert dotted["principal"] == "dns:ops.corp.example"
    assert dotted["principal"] == plain["principal"]


def test_forwarded_chain_requires_trusted_proxy():
    m = mapper()
    forwarded = chain_for("spiffe://corp.example/dept-a/ops", "fp-forwarded")
    request = {"peer_chain": chain_for("spiffe://corp.example/gateway/edge", "fp-edge"),
               "forwarded_chain": forwarded, "source": "10.0.0.5"}
    with pytest.raises(ValueError):
        m.map_request(request)
    trusted = mapper(trusted_proxies={"10.0.0.5"})
    result = trusted.map_request(request)
    assert result["principal"] == "spiffe://corp.example/dept-a/ops"
    assert result["source"] == "10.0.0.5"
