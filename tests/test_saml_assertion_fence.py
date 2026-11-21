import base64
import datetime
import xml.etree.ElementTree as ET

import pytest

import saml_assertion_fence as sf

SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
DS = "http://www.w3.org/2000/09/xmldsig#"

ET.register_namespace("samlp", SAMLP)
ET.register_namespace("saml", SAML)
ET.register_namespace("ds", DS)

IDP = "https://idp.example.com"
SP = "https://sp.example.com"
ACS = "https://sp.example.com/acs"

METADATA = {"entity_id": IDP, "valid_until": 10000, "certificate": b"cert"}


class Verifier:
    def __init__(self):
        self.calls = 0

    def __call__(self, element_bytes, signature, digest_algorithm):
        self.calls += 1
        return signature == "good"


def iso(ts):
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sub(parent, tag, ns=SAML, text=None, **attrs):
    element = ET.SubElement(parent, "{%s}%s" % (ns, tag),
                            {key: str(value) for key, value in attrs.items()})
    if text is not None:
        element.text = text
    return element


def assertion(assertion_id="_a1", issuer=IDP, subject="alice", audience=SP,
              recipient=ACS, in_response_to="_req1", not_before=100,
              not_on_or_after=400, session_index="s1", sign=True, ref_id=None,
              digest="sha256", signature="good"):
    element = ET.Element("{%s}Assertion" % SAML,
                         {"ID": assertion_id, "IssueInstant": iso(not_before)})
    sub(element, "Issuer", text=issuer)
    if sign:
        sig = sub(element, "Signature", ns=DS)
        signed = sub(sig, "SignedInfo", ns=DS)
        reference = sub(signed, "Reference", ns=DS, URI="#" + (ref_id or assertion_id))
        sub(reference, "DigestMethod", ns=DS,
            Algorithm="http://www.w3.org/2001/04/xmlenc#" + digest)
        sub(sig, "SignatureValue", ns=DS, text=signature)
    subject_element = sub(element, "Subject")
    sub(subject_element, "NameID", text=subject)
    conditions = sub(element, "Conditions", NotBefore=iso(not_before),
                     NotOnOrAfter=iso(not_on_or_after))
    restriction = sub(conditions, "AudienceRestriction")
    sub(restriction, "Audience", text=audience)
    confirmation = sub(element, "SubjectConfirmation",
                       Method="urn:oasis:names:tc:SAML:2.0:cm:bearer")
    sub(confirmation, "SubjectConfirmationData", Recipient=recipient,
        InResponseTo=in_response_to, NotOnOrAfter=iso(not_on_or_after))
    sub(element, "AuthnStatement", SessionIndex=session_index)
    return element


def response(assertions=None, in_response_to="_req1", destination=ACS,
             issuer=IDP, response_signature=False, now=200):
    root = ET.Element("{%s}Response" % SAMLP, {
        "ID": "_resp",
        "InResponseTo": in_response_to,
        "Destination": destination,
        "IssueInstant": iso(now),
    })
    sub(root, "Issuer", text=issuer)
    if response_signature:
        sig = sub(root, "Signature", ns=DS)
        signed = sub(sig, "SignedInfo", ns=DS)
        reference = sub(signed, "Reference", ns=DS, URI="#_resp")
        sub(reference, "DigestMethod", ns=DS,
            Algorithm="http://www.w3.org/2001/04/xmlenc#sha256")
        sub(sig, "SignatureValue", ns=DS, text="good")
    for item in assertions or []:
        root.append(item)
    return ET.tostring(root, encoding="utf-8")


def encrypted_response(ciphertext, in_response_to="_req1", destination=ACS,
                       issuer=IDP, now=200):
    root = ET.Element("{%s}Response" % SAMLP, {
        "ID": "_resp",
        "InResponseTo": in_response_to,
        "Destination": destination,
        "IssueInstant": iso(now),
    })
    sub(root, "Issuer", text=issuer)
    encrypted = sub(root, "EncryptedAssertion")
    cipher = sub(encrypted, "CipherData")
    sub(cipher, "CipherValue",
        text=base64.b64encode(ciphertext).decode("ascii"))
    return ET.tostring(root, encoding="utf-8")


def fence(**overrides):
    verifier = Verifier()
    args = {
        "entity_id": SP,
        "acs_url": ACS,
        "metadata": dict(METADATA),
        "verifier": verifier,
    }
    args.update(overrides)
    guard = sf.SamlAssertionFence(**args)
    return guard, verifier


def test_valid_response_is_accepted():
    guard, verifier = fence()
    result = guard.process(response([assertion()]), "_req1", 200)
    assert result["subject"] == "alice"
    assert result["session_index"] == "s1"
    assert result["assertion_id"] == "_a1"
    assert verifier.calls == 1


def test_multiple_assertions_are_rejected():
    guard, verifier = fence()
    document = response([assertion("_a1"), assertion("_a2")])
    with pytest.raises(ValueError):
        guard.process(document, "_req1", 200)
    assert verifier.calls == 0


def test_signature_reference_id_must_match_assertion_id():
    guard, verifier = fence()
    document = response([assertion("_a1", ref_id="_a2")])
    with pytest.raises(ValueError):
        guard.process(document, "_req1", 200)
    assert verifier.calls == 0


def test_response_signature_does_not_cover_unsigned_assertion():
    guard, verifier = fence()
    document = response([assertion(sign=False)], response_signature=True)
    with pytest.raises(ValueError):
        guard.process(document, "_req1", 200)
    assert verifier.calls == 0


def test_audience_must_match_exactly():
    guard, verifier = fence()
    document = response([assertion(audience=SP + ".evil.com")])
    with pytest.raises(ValueError):
        guard.process(document, "_req1", 200)


def test_in_response_to_must_match_the_request():
    guard, verifier = fence()
    document = response([assertion()])
    with pytest.raises(ValueError):
        guard.process(document, "_req2", 200)


def test_destination_must_match_acs():
    guard, verifier = fence()
    document = response([assertion()], destination=SP + "/other")
    with pytest.raises(ValueError):
        guard.process(document, "_req1", 200)


def test_recipient_must_match_acs():
    guard, verifier = fence()
    document = response([assertion(recipient=SP + "/other")])
    with pytest.raises(ValueError):
        guard.process(document, "_req1", 200)


def test_not_before_boundary_is_inclusive():
    guard, verifier = fence()
    document = response([assertion(not_before=200, not_on_or_after=400)], now=200)
    with pytest.raises(ValueError):
        guard.process(document, "_req1", 199)
    accepted = guard.process(document, "_req1", 200)
    assert accepted["subject"] == "alice"


def test_not_on_or_after_boundary_is_exclusive():
    early, _ = fence()
    document = response([assertion(not_before=100, not_on_or_after=300)], now=299)
    assert early.process(document, "_req1", 299)["subject"] == "alice"
    late, _ = fence()
    document2 = response([assertion(not_before=100, not_on_or_after=300)], now=300)
    with pytest.raises(ValueError):
        late.process(document2, "_req1", 300)


def test_sha1_digest_algorithm_is_rejected():
    guard, verifier = fence()
    document = response([assertion(digest="sha1")])
    with pytest.raises(ValueError):
        guard.process(document, "_req1", 200)
    assert verifier.calls == 0


def test_issuer_must_match_metadata_entity():
    guard, verifier = fence()
    document = response([assertion(issuer="https://idp.evil.com")])
    with pytest.raises(ValueError):
        guard.process(document, "_req1", 200)


def test_expired_metadata_is_rejected():
    guard, verifier = fence(metadata={
        "entity_id": IDP,
        "valid_until": 150,
        "certificate": b"cert",
    })
    document = response([assertion()])
    with pytest.raises(ValueError):
        guard.process(document, "_req1", 200)


def test_assertion_is_single_use_and_logout_invalidates():
    guard, verifier = fence()
    first = response([assertion("_a1")])
    assert guard.process(first, "_req1", 200)["subject"] == "alice"
    with pytest.raises(ValueError):
        guard.process(first, "_req1", 201)
    second = response([assertion("_a2")])
    assert guard.process(second, "_req1", 202)["subject"] == "alice"
    guard.logout("_a2")
    with pytest.raises(ValueError):
        guard.process(second, "_req1", 203)


def test_encrypted_assertion_key_is_selected_by_receiver():
    plaintext = ET.tostring(assertion(), encoding="utf-8")

    def decryptor(ciphertext, key):
        if key == b"ka":
            return plaintext
        return None

    guard, verifier = fence(decryptor=decryptor)
    guard.set_decryption_key("sp-a", b"ka")
    guard.set_decryption_key("sp-b", b"kb")
    document = encrypted_response(plaintext)
    with pytest.raises(ValueError):
        guard.process(document, "_req1", 200, receiver="sp-b")
    result = guard.process(document, "_req1", 200, receiver="sp-a")
    assert result["subject"] == "alice"
