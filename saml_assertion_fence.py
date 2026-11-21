"""SAML assertion validation fence with strict single-assertion handling."""

import base64
import datetime
import xml.etree.ElementTree as ET


SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
DS = "http://www.w3.org/2000/09/xmldsig#"

RESPONSE_TAG = "{%s}Response" % SAMLP
ASSERTION_TAG = "{%s}Assertion" % SAML
ENCRYPTED_ASSERTION_TAG = "{%s}EncryptedAssertion" % SAML

BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


def _text(element):
    return "" if element is None or element.text is None else element.text.strip()


def _single_child(parent, tag, description):
    matches = parent.findall(tag)
    if len(matches) != 1:
        raise ValueError("expected exactly one %s" % description)
    return matches[0]


def _parse_instant(value, description):
    if value is None:
        raise ValueError("missing %s" % description)
    text = str(value).strip()
    try:
        if "T" not in text:
            return float(text)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid %s" % description)


def _as_timestamp(value, description):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.timestamp()
    return _parse_instant(value, description)


def _digest_name(algorithm):
    if not algorithm:
        raise ValueError("missing digest algorithm")
    name = algorithm.rsplit("#", 1)[-1].strip().lower()
    if not name:
        raise ValueError("invalid digest algorithm")
    return name


class SamlAssertionFence:
    """Validates signed SAML responses against metadata and request state."""

    def __init__(self, entity_id, acs_url, metadata, verifier=None,
                 decryptor=None, allowed_digests=("sha256", "sha384", "sha512")):
        if not entity_id:
            raise ValueError("entity ID is required")
        if not acs_url:
            raise ValueError("ACS URL is required")
        self.entity_id = entity_id
        self.acs_url = acs_url
        self.metadata = dict(metadata or {})
        self.verifier = verifier
        self.decryptor = decryptor
        self.allowed_digests = frozenset(
            _digest_name(item) if "#" in str(item) else str(item).strip().lower()
            for item in allowed_digests
        )
        self._decryption_keys = {}
        self._consumed_assertions = set()

    def set_decryption_key(self, receiver, key):
        if not receiver:
            raise ValueError("receiver is required")
        self._decryption_keys[receiver] = key

    def process(self, response_xml, request_id, now, receiver=None):
        if not request_id:
            raise ValueError("request ID is required")
        now = _as_timestamp(now, "current time")
        self._validate_metadata(now)

        root = self._parse_response(response_xml)
        self._validate_response(root, request_id)

        assertion = self._locate_unique_assertion(root)
        if assertion.tag == ENCRYPTED_ASSERTION_TAG:
            assertion = self._decrypt_assertion(assertion, receiver)

        assertion_id = assertion.get("ID")
        if not assertion_id:
            raise ValueError("assertion ID is required")
        if assertion_id in self._consumed_assertions:
            raise ValueError("assertion has already been consumed")

        signed_xml, signature, digest = self._validate_signature(
            assertion, assertion_id)
        self._verify_signature(signed_xml, signature, digest)

        subject, session_index = self._validate_assertion(
            assertion, request_id, now)

        self._consumed_assertions.add(assertion_id)
        return {
            "subject": subject,
            "session_index": session_index,
            "assertion_id": assertion_id,
        }

    def logout(self, assertion_id, now=0):
        if not assertion_id:
            raise ValueError("assertion ID is required")
        self._consumed_assertions.add(assertion_id)

    def snapshot(self):
        return {
            "entity_id": self.entity_id,
            "acs_url": self.acs_url,
            "consumed_assertions": sorted(self._consumed_assertions),
            "decryption_receivers": sorted(self._decryption_keys),
        }

    def _parse_response(self, response_xml):
        try:
            return ET.fromstring(response_xml)
        except (ET.ParseError, TypeError, ValueError):
            raise ValueError("invalid SAML response XML")

    def _validate_metadata(self, now):
        if not self.metadata.get("entity_id"):
            raise ValueError("metadata entity ID is required")
        valid_until = self.metadata.get("valid_until")
        if valid_until is None:
            raise ValueError("metadata validity is required")
        if now >= _as_timestamp(valid_until, "metadata valid_until"):
            raise ValueError("metadata has expired")

    def _validate_response(self, root, request_id):
        if root.tag != RESPONSE_TAG:
            raise ValueError("expected a SAML Response")
        if root.get("InResponseTo") != request_id:
            raise ValueError("response InResponseTo does not match the request")
        if root.get("Destination") != self.acs_url:
            raise ValueError("response Destination does not match the ACS")

        issuer = _single_child(root, "{%s}Issuer" % SAML, "response Issuer")
        if _text(issuer) != self.metadata["entity_id"]:
            raise ValueError("response Issuer does not match metadata")

    def _locate_unique_assertion(self, root):
        plain_assertions = [
            child for child in list(root) if child.tag == ASSERTION_TAG
        ]
        encrypted_assertions = [
            child for child in list(root)
            if child.tag == ENCRYPTED_ASSERTION_TAG
        ]
        if len(plain_assertions) + len(encrypted_assertions) != 1:
            raise ValueError("response must contain exactly one assertion")

        nested_assertions = root.findall(".//%s" % ASSERTION_TAG)
        if len(nested_assertions) != len(plain_assertions):
            raise ValueError("nested assertions are not allowed")

        return (plain_assertions or encrypted_assertions)[0]

    def _decrypt_assertion(self, encrypted_assertion, receiver):
        if self.decryptor is None:
            raise ValueError("encrypted assertion requires a decryptor")
        if not receiver or receiver not in self._decryption_keys:
            raise ValueError("no decryption key for receiver")

        cipher_data = [
            child for child in list(encrypted_assertion)
            if _local_name(child.tag) == "CipherData"
        ]
        if len(cipher_data) != 1:
            raise ValueError("encrypted assertion has invalid cipher data")
        cipher_values = [
            child for child in list(cipher_data[0])
            if _local_name(child.tag) == "CipherValue"
        ]
        if len(cipher_values) != 1:
            raise ValueError("encrypted assertion has invalid cipher value")

        encoded = "".join(_text(cipher_values[0]).split())
        try:
            ciphertext = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise ValueError("encrypted assertion ciphertext is invalid")

        try:
            decrypted = self.decryptor(
                ciphertext, self._decryption_keys[receiver])
        except Exception:
            raise ValueError("encrypted assertion could not be decrypted")
        if decrypted is None:
            raise ValueError("encrypted assertion could not be decrypted")

        if isinstance(decrypted, ET.Element):
            assertion = decrypted
        else:
            try:
                assertion = ET.fromstring(decrypted)
            except (ET.ParseError, TypeError, ValueError):
                raise ValueError("decrypted assertion is invalid XML")
        if assertion.tag != ASSERTION_TAG:
            raise ValueError("decrypted payload is not a SAML Assertion")
        return assertion

    def _validate_signature(self, assertion, assertion_id):
        signatures = assertion.findall("{%s}Signature" % DS)
        if len(signatures) != 1:
            raise ValueError("assertion must have exactly one signature")
        signature_element = signatures[0]

        signed_info = _single_child(
            signature_element, "{%s}SignedInfo" % DS, "SignedInfo")
        references = signed_info.findall("{%s}Reference" % DS)
        if len(references) != 1:
            raise ValueError("signature must have exactly one reference")
        reference = references[0]

        if reference.get("URI") != "#" + assertion_id:
            raise ValueError(
                "signature reference does not match the assertion ID")

        digest_method = _single_child(
            reference, "{%s}DigestMethod" % DS, "DigestMethod")
        digest = _digest_name(digest_method.get("Algorithm"))
        if digest not in self.allowed_digests:
            raise ValueError("weak or unsupported digest algorithm")

        signature_value = _single_child(
            signature_element, "{%s}SignatureValue" % DS, "SignatureValue")
        signature = _text(signature_value)
        if not signature:
            raise ValueError("signature value is empty")

        return ET.tostring(assertion, encoding="utf-8"), signature, digest

    def _verify_signature(self, signed_xml, signature, digest):
        if self.verifier is None:
            raise ValueError("no signature verifier configured")
        try:
            valid = self.verifier(signed_xml, signature, digest)
        except Exception:
            raise ValueError("signature verification failed")
        if not valid:
            raise ValueError("signature verification failed")

    def _validate_assertion(self, assertion, request_id, now):
        if assertion.tag != ASSERTION_TAG:
            raise ValueError("expected a SAML Assertion")

        issuer = _single_child(assertion, "{%s}Issuer" % SAML, "assertion Issuer")
        if _text(issuer) != self.metadata["entity_id"]:
            raise ValueError("assertion Issuer does not match metadata")

        subject_element = _single_child(
            assertion, "{%s}Subject" % SAML, "assertion Subject")
        name_id = _single_child(
            subject_element, "{%s}NameID" % SAML, "subject NameID")
        subject = _text(name_id)
        if not subject:
            raise ValueError("subject NameID is empty")

        self._validate_conditions(assertion, now)
        self._validate_subject_confirmations(assertion, request_id, now)

        session_index = None
        authn_statement = assertion.find("{%s}AuthnStatement" % SAML)
        if authn_statement is not None:
            session_index = authn_statement.get("SessionIndex")

        return subject, session_index

    def _validate_conditions(self, assertion, now):
        conditions = _single_child(
            assertion, "{%s}Conditions" % SAML, "assertion Conditions")
        not_before = _parse_instant(
            conditions.get("NotBefore"), "Conditions NotBefore")
        not_on_or_after = _parse_instant(
            conditions.get("NotOnOrAfter"), "Conditions NotOnOrAfter")
        if not_before > not_on_or_after:
            raise ValueError("assertion validity interval is invalid")
        if now < not_before or now >= not_on_or_after:
            raise ValueError("assertion is not currently valid")

        restrictions = conditions.findall(
            "{%s}AudienceRestriction" % SAML)
        if not restrictions:
            raise ValueError("assertion has no audience restriction")
        for restriction in restrictions:
            audiences = restriction.findall("{%s}Audience" % SAML)
            if not any(_text(audience) == self.entity_id
                       for audience in audiences):
                raise ValueError("assertion audience does not match")

    def _validate_subject_confirmations(self, assertion, request_id, now):
        confirmations = [
            element for element in assertion.iter(
                "{%s}SubjectConfirmation" % SAML)
            if element.get("Method") == BEARER
        ]
        if not confirmations:
            raise ValueError("assertion has no bearer subject confirmation")

        for confirmation in confirmations:
            data = _single_child(
                confirmation,
                "{%s}SubjectConfirmationData" % SAML,
                "SubjectConfirmationData",
            )
            if data.get("Recipient") != self.acs_url:
                raise ValueError(
                    "subject confirmation Recipient does not match the ACS")
            if data.get("InResponseTo") != request_id:
                raise ValueError(
                    "subject confirmation InResponseTo does not match")

            not_on_or_after = _parse_instant(
                data.get("NotOnOrAfter"),
                "SubjectConfirmationData NotOnOrAfter",
            )
            if now >= not_on_or_after:
                raise ValueError("subject confirmation has expired")

            not_before = data.get("NotBefore")
            if not_before is not None:
                if now < _parse_instant(
                        not_before, "SubjectConfirmationData NotBefore"):
                    raise ValueError(
                        "subject confirmation is not yet valid")
