"""Mutual-TLS certificate-chain to service-principal mapping.

The certificate representation used by this module is deliberately small and
JSON-friendly.  A certificate is a mapping with these fields::

    {
        "fingerprint": "sha256:...",
        "subject": "CN=ops,O=corp",
        "issuer": "CN=inter,O=corp",
        "cn": "ops",
        "san_uri": ["spiffe://corp.example/dept-a/ops"],
        "san_dns": ["ops.corp.example"],
        "critical_extensions": ["2.5.29.17"],
        "not_before": 0,
        "not_after": 100,
        "self_signed": False,
    }

Chains are ordered leaf first.  Trust anchors may be supplied directly with
``add_root`` or by name with ``add_trust_bundle``.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit


_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_PERCENT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")

# Critical extensions understood by this small policy layer.  Deployments can
# extend this set through ``known_critical_extensions``.
_DEFAULT_CRITICAL_EXTENSIONS = frozenset(
    {
        "2.5.29.9",   # subjectDirectoryAttributes
        "2.5.29.14",  # subjectKeyIdentifier
        "2.5.29.15",  # keyUsage
        "2.5.29.16",  # privateKeyUsagePeriod
        "2.5.29.17",  # subjectAltName
        "2.5.29.18",  # issuerAltName
        "2.5.29.19",  # basicConstraints
        "2.5.29.20",  # cRLNumber
        "2.5.29.21",  # reasonCode
        "2.5.29.23",  # holdInstructionCode
        "2.5.29.24",  # invalidityDate
        "2.5.29.27",  # deltaCRLIndicator
        "2.5.29.28",  # issuingDistributionPoint
        "2.5.29.29",  # certificateIssuer
        "2.5.29.30",  # nameConstraints
        "2.5.29.31",  # cRLDistributionPoints
        "2.5.29.32",  # certificatePolicies
        "2.5.29.33",  # policyMappings
        "2.5.29.35",  # authorityKeyIdentifier
        "2.5.29.36",  # policyConstraints
        "2.5.29.37",  # extKeyUsage
        "2.5.29.46",  # freshestCRL
        "2.5.29.54",  # inhibitAnyPolicy
    }
)


def _cert_value(cert, *names, default=None):
    """Read a certificate field from either a mapping or an object."""

    if isinstance(cert, Mapping):
        for name in names:
            if name in cert:
                return cert[name]
        return default
    for name in names:
        if hasattr(cert, name):
            return getattr(cert, name)
    return default


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _normalize_dns_name(name):
    """Normalize a DNS name for identity comparison.

    DNS names are case-insensitive and an absolute name's trailing dot is not
    part of its identity.  More than one trailing dot is malformed.
    """

    if not isinstance(name, str):
        raise ValueError("DNS name must be a string")
    value = name.strip().lower()
    if not value:
        raise ValueError("DNS name must not be empty")
    if value.endswith("."):
        value = value[:-1]
    if (
        not value
        or value.endswith(".")
        or value.startswith(".")
        or ".." in value
    ):
        raise ValueError("invalid DNS name")
    if any(ch.isspace() for ch in value):
        raise ValueError("invalid DNS name")
    return value


def _normalize_cn(name):
    if not isinstance(name, str):
        raise ValueError("common name must be a string")
    value = name.strip().lower()
    if value.endswith("."):
        value = value[:-1]
    if not value or value.endswith("."):
        raise ValueError("invalid common name")
    return value


def _is_wildcard_domain(value):
    return (
        value.startswith("*.")
        and value.count("*") == 1
        and len(value) > 2
        and not value[2:].startswith(".")
    )


def _canonicalize_percent_encoding(value):
    """Decode only RFC 3986 unreserved octets; preserve reserved escapes."""

    if _INVALID_PERCENT_ESCAPE.search(value):
        raise ValueError("invalid percent escape in URI")

    def replace(match):
        octet = int(match.group(1), 16)
        character = chr(octet)
        if character in _UNRESERVED:
            return character
        return "%" + match.group(1).upper()

    return _PERCENT_ESCAPE.sub(replace, value)


def _normalize_netloc(netloc):
    """Lower-case the host portion of a URI authority without touching userinfo."""

    userinfo, separator, hostport = netloc.rpartition("@")
    prefix = userinfo + separator if separator else ""

    if hostport.startswith("["):
        closing = hostport.find("]")
        if closing == -1:
            raise ValueError("invalid IPv6 URI authority")
        host = hostport[1:closing].lower()
        remainder = hostport[closing + 1 :]
        return f"{prefix}[{host}]{remainder}"

    host, colon, port = hostport.partition(":")
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host:
        raise ValueError("URI authority has no host")
    return f"{prefix}{host}{colon}{port}"


class MtlsPrincipalMap:
    """Derive a service principal from a verified mutual-TLS certificate chain."""

    def __init__(
        self,
        trusted_proxies=None,
        trust_domains=None,
        allow_cn_fallback=False,
        revocation_check_interval=300,
        cache_ttl=60,
        known_critical_extensions=None,
    ):
        if revocation_check_interval is None or revocation_check_interval < 0:
            raise ValueError("revocation_check_interval must be non-negative")
        if cache_ttl is None or cache_ttl < 0:
            raise ValueError("cache_ttl must be non-negative")

        if isinstance(trusted_proxies, str):
            configured_proxies = [trusted_proxies]
        else:
            configured_proxies = trusted_proxies or []
        self.trusted_proxies = {
            str(proxy).strip()
            for proxy in configured_proxies
            if str(proxy).strip()
        }
        self._trusted_proxy_addresses = self._parse_trusted_proxy_addresses(
            self.trusted_proxies
        )
        self._trusted_proxy_networks = self._parse_trusted_proxy_networks(
            self.trusted_proxies
        )

        if trust_domains is None:
            self.trust_domains = None
            self._trust_domains = None
        else:
            domains = [
                domain
                for domain in (
                    trust_domains
                    if isinstance(trust_domains, (list, tuple, set, frozenset))
                    else [trust_domains]
                )
                if str(domain).strip()
            ]
            self.trust_domains = set(domains)
            self._trust_domains = {
                self._normalize_trust_domain(domain) for domain in domains
            }

        self.allow_cn_fallback = bool(allow_cn_fallback)
        self.revocation_check_interval = revocation_check_interval
        self.cache_ttl = cache_ttl
        self.known_critical_extensions = set(_DEFAULT_CRITICAL_EXTENSIONS)
        for extension in _as_list(known_critical_extensions):
            extension = str(extension).strip()
            if extension:
                self.known_critical_extensions.add(extension)

        self._trust_anchors = {}
        self._trust_bundles = {}
        self._revocations = {}

    @staticmethod
    def _parse_trusted_proxy_addresses(proxies):
        addresses = set()
        for proxy in proxies:
            if "/" in proxy:
                continue
            try:
                addresses.add(ipaddress.ip_address(proxy))
            except ValueError:
                continue
        return frozenset(addresses)

    @staticmethod
    def _parse_trusted_proxy_networks(proxies):
        networks = []
        for proxy in proxies:
            if "/" not in proxy:
                continue
            try:
                networks.append(ipaddress.ip_network(proxy, strict=False))
            except ValueError:
                continue
        return tuple(networks)

    @staticmethod
    def _normalize_trust_domain(domain):
        value = str(domain).strip()
        if "://" in value:
            parsed = urlsplit(value)
            value = parsed.hostname or parsed.netloc
            if not value:
                raise ValueError("trust domain must not be empty")
        value = value.lower()
        if value.endswith("."):
            value = value[:-1]
        if not value:
            raise ValueError("trust domain must not be empty")
        if "*" in value and not _is_wildcard_domain(value):
            raise ValueError("invalid wildcard trust domain")
        return value

    def add_root(self, cert):
        """Add one explicitly trusted root/anchor."""

        self._add_anchor(cert)

    def add_trust_bundle(self, name, certs):
        """Add every anchor in a named bundle.

        Bundles are additive: adding a rotation bundle leaves existing roots in
        place, so certificates issued by either root remain valid during a
        rotation window.
        """

        if not isinstance(name, str) or not name.strip():
            raise ValueError("trust bundle name must not be empty")

        if isinstance(certs, Mapping) or not isinstance(certs, Iterable):
            bundle = [certs]
        else:
            bundle = list(certs)
        if not bundle:
            raise ValueError("trust bundle must not be empty")

        anchors = [self._add_anchor(cert) for cert in bundle]
        self._trust_bundles[name.strip()] = anchors

    def _add_anchor(self, cert):
        fingerprint = self._cert_fingerprint(cert)
        if fingerprint in self._trust_anchors:
            existing = self._trust_anchors[fingerprint]
            if self._cert_subject(existing) != self._cert_subject(cert):
                raise ValueError("conflicting certificate for trust anchor")
        copied = dict(cert) if isinstance(cert, Mapping) else cert
        self._trust_anchors[fingerprint] = copied
        return copied

    def revoke(self, fingerprint, at=0):
        """Mark a certificate fingerprint as revoked at time ``at``."""

        fingerprint = str(fingerprint).strip()
        if not fingerprint:
            raise ValueError("fingerprint must not be empty")
        if at is None:
            at = 0
        previous = self._revocations.get(fingerprint)
        if previous is None or at < previous:
            self._revocations[fingerprint] = at

    def canonical_uri(self, uri):
        """Canonicalize a URI SAN.

        Percent-encoded unreserved characters are decoded, other escapes keep
        their existing meaning with upper-case hex digits.  Scheme and host are
        case-insensitive; the path and query remain case-sensitive.
        """

        if not isinstance(uri, str):
            raise ValueError("URI must be a string")
        value = uri.strip()
        if not value:
            raise ValueError("URI must not be empty")

        value = _canonicalize_percent_encoding(value)
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ValueError("invalid URI") from exc
        if not parsed.scheme:
            raise ValueError("URI must include a scheme")

        scheme = parsed.scheme.lower()
        if parsed.netloc:
            netloc = _normalize_netloc(parsed.netloc)
            result = f"{scheme}://{netloc}{parsed.path}"
        else:
            result = f"{scheme}:{parsed.path}"

        if parsed.query:
            result += "?" + parsed.query
        if parsed.fragment:
            result += "#" + parsed.fragment
        return result

    def match_dns(self, pattern, hostname):
        """Match a DNS SAN pattern.

        A wildcard is only valid as the complete left-most label and matches
        exactly one label.  It cannot match an apex or cross a label boundary.
        """

        try:
            normalized_pattern = _normalize_dns_name(pattern)
            normalized_host = _normalize_dns_name(hostname)
        except ValueError:
            return False

        if "*" not in normalized_pattern:
            return normalized_pattern == normalized_host

        if "*" in normalized_host:
            return False

        if (
            not normalized_pattern.startswith("*.")
            or normalized_pattern.count("*") != 1
        ):
            return False

        suffix = normalized_pattern[2:]
        if not suffix or "*" in suffix:
            return False

        host_labels = normalized_host.split(".")
        suffix_labels = suffix.split(".")
        return (
            len(host_labels) == len(suffix_labels) + 1
            and host_labels[1:] == suffix_labels
            and bool(host_labels[0])
        )

    def authorize_host(self, cert, hostname):
        """Return whether a certificate authorizes the given DNS hostname."""

        dns_names = [
            name
            for name in _as_list(_cert_value(cert, "san_dns", "dns_sans"))
            if isinstance(name, str) and name.strip()
        ]
        if dns_names:
            return any(self.match_dns(name, hostname) for name in dns_names)

        if self.allow_cn_fallback:
            cn = self._cert_cn(cert)
            if cn:
                return self.match_dns(cn, hostname)
        return False

    def map_request(self, request, now=0):
        """Validate a request's certificate chain and derive its principal."""

        if not isinstance(request, Mapping):
            raise ValueError("request must be a mapping")
        if now is None:
            now = 0

        forwarded = request.get("forwarded_chain")
        using_forwarded = forwarded is not None

        if using_forwarded:
            source = request.get("source")
            if not self._is_trusted_proxy(source):
                raise ValueError("forwarded certificate chain is not trusted")
            supplied_chain = forwarded
        else:
            supplied_chain = request.get("peer_chain")

        chain = self._verify_chain(supplied_chain, now)

        for cert in chain:
            if self._is_revoked(self._cert_fingerprint(cert), now):
                raise ValueError("certificate is revoked")

        leaf = chain[0]
        result = {
            "principal": self._principal_for(leaf),
            "chain": [self._cert_fingerprint(cert) for cert in chain],
            "fingerprint": self._cert_fingerprint(leaf),
            "not_before": self._cert_not_before(leaf),
            "expires_at": self._cert_not_after(leaf),
            "status": "ok",
            "last_revocation_check": now,
            "revocation_check_interval": self.revocation_check_interval,
        }
        if using_forwarded:
            result["source"] = request.get("source")
        return result

    def revalidate(self, mapping, now):
        """Revalidate a cached mapping for a long-lived connection."""

        if not isinstance(mapping, Mapping):
            raise ValueError("mapping must be a mapping")
        if now is None:
            now = 0

        if mapping.get("status") == "revoked":
            return mapping

        expires_at = mapping.get("expires_at")
        if expires_at is not None and now >= expires_at:
            mapping["status"] = "expired"
            return mapping

        interval = mapping.get(
            "revocation_check_interval", self.revocation_check_interval
        )
        if interval is None or interval < 0:
            interval = self.revocation_check_interval

        last_check = mapping.get("last_revocation_check", 0)
        if last_check is None:
            last_check = 0

        if now >= last_check + interval:
            chain = mapping.get("chain")
            if not chain:
                chain = [mapping.get("fingerprint")]
            if any(self._is_revoked(fingerprint, now) for fingerprint in chain):
                mapping["status"] = "revoked"
            else:
                mapping["status"] = "ok"
            mapping["last_revocation_check"] = now
        else:
            mapping["status"] = "ok"
        return mapping

    def _verify_chain(self, supplied_chain, now):
        if not isinstance(supplied_chain, (list, tuple)) or not supplied_chain:
            raise ValueError("peer certificate chain must not be empty")
        if len(supplied_chain) > 16:
            raise ValueError("certificate chain is too long")

        chain = []
        seen_fingerprints = set()
        for cert in supplied_chain:
            self._validate_certificate(cert, now)
            fingerprint = self._cert_fingerprint(cert)
            if fingerprint in seen_fingerprints:
                raise ValueError("duplicate certificate in chain")
            seen_fingerprints.add(fingerprint)
            chain.append(cert)

        for leaf, issuer in zip(chain, chain[1:]):
            if self._cert_issuer(leaf) != self._cert_subject(issuer):
                raise ValueError("certificate chain issuer/subject mismatch")

        for cert in chain:
            fingerprint = self._cert_fingerprint(cert)
            if (
                self._is_self_signed(cert)
                and fingerprint not in self._trust_anchors
            ):
                raise ValueError("untrusted self-signed certificate in chain")

        terminal = chain[-1]
        terminal_fingerprint = self._cert_fingerprint(terminal)
        if terminal_fingerprint not in self._trust_anchors:
            anchor = self._find_anchor(terminal)
            if anchor is None:
                raise ValueError("certificate chain does not reach a trust anchor")
            self._validate_certificate(anchor, now)
            anchor_fingerprint = self._cert_fingerprint(anchor)
            if anchor_fingerprint in seen_fingerprints:
                raise ValueError("duplicate certificate in chain")
            chain.append(anchor)

        return chain

    def _find_anchor(self, terminal):
        issuer = self._cert_issuer(terminal)
        subject_matches = [
            anchor
            for anchor in self._trust_anchors.values()
            if self._cert_subject(anchor) == issuer
        ]
        authority_key_id = _cert_value(
            terminal, "authority_key_identifier", "aki"
        )
        if authority_key_id is not None:
            key_matches = []
            for anchor in self._trust_anchors.values():
                subject_key_id = _cert_value(
                    anchor, "subject_key_identifier", "ski"
                )
                if (
                    subject_key_id is not None
                    and str(subject_key_id) == str(authority_key_id)
                ):
                    key_matches.append(anchor)
            if key_matches:
                for anchor in key_matches:
                    if self._cert_subject(anchor) == issuer:
                        return anchor
                return None
        return subject_matches[0] if subject_matches else None

    def _validate_certificate(self, cert, now):
        self._cert_fingerprint(cert)
        self._cert_subject(cert)
        self._cert_issuer(cert)

        if now < self._cert_not_before(cert) or now >= self._cert_not_after(cert):
            raise ValueError("certificate is not valid at this time")

        critical_extensions = _as_list(
            _cert_value(cert, "critical_extensions", "critical", default=[])
        )
        for extension in critical_extensions:
            if isinstance(extension, Mapping):
                if not extension.get("critical", True):
                    continue
                oid = extension.get("oid", extension.get("id"))
            else:
                oid = extension
            oid = str(oid).strip()
            if oid and oid not in self.known_critical_extensions:
                raise ValueError(
                    f"unknown critical certificate extension: {oid}"
                )

    def _principal_for(self, cert):
        uris = [
            self.canonical_uri(uri)
            for uri in _as_list(_cert_value(cert, "san_uri", "uri_sans"))
            if isinstance(uri, str) and uri.strip()
        ]

        if uris:
            if self._trust_domains is not None:
                uris = [uri for uri in uris if self._uri_is_trusted(uri)]
                if not uris:
                    raise ValueError("URI SAN is outside the trusted domains")
            distinct_uris = list(dict.fromkeys(uris))
            if len(distinct_uris) != 1:
                raise ValueError("certificate has ambiguous URI SAN identities")
            return distinct_uris[0]

        dns_names = [
            _normalize_dns_name(name)
            for name in _as_list(_cert_value(cert, "san_dns", "dns_sans"))
            if isinstance(name, str) and name.strip()
        ]
        if dns_names:
            return "dns:" + dns_names[0]

        if self.allow_cn_fallback:
            cn = self._cert_cn(cert)
            if cn:
                return "cn:" + _normalize_cn(cn)

        raise ValueError("certificate has no usable service identity")

    def _uri_is_trusted(self, uri):
        if self._trust_domains is None:
            return True
        try:
            parsed = urlsplit(uri)
        except ValueError:
            return False
        if not parsed.hostname:
            return False
        hostname = parsed.hostname.lower()
        if hostname.endswith("."):
            hostname = hostname[:-1]
        for domain in self._trust_domains:
            if domain.startswith("*."):
                if self.match_dns(domain, hostname):
                    return True
            elif hostname == domain:
                return True
        return False

    def _is_trusted_proxy(self, source):
        if source is None:
            return False
        if isinstance(source, (tuple, list)):
            if not source:
                return False
            source = source[0]

        source_text = str(source).strip()
        if not source_text:
            return False
        if source_text in self.trusted_proxies:
            return True

        address = self._source_address(source_text)
        if address is None:
            return False
        return (
            address in self._trusted_proxy_addresses
            or any(address in network for network in self._trusted_proxy_networks)
        )

    @staticmethod
    def _source_address(source):
        value = source
        if value.startswith("["):
            closing = value.find("]")
            if closing != -1:
                value = value[1:closing]
        elif value.count(":") == 1:
            host, port = value.rsplit(":", 1)
            if port.isdigit():
                value = host
        try:
            return ipaddress.ip_address(value)
        except ValueError:
            return None

    def _is_revoked(self, fingerprint, now):
        revoked_at = self._revocations.get(fingerprint)
        return revoked_at is not None and revoked_at <= now

    @staticmethod
    def _cert_fingerprint(cert):
        fingerprint = _cert_value(cert, "fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("certificate fingerprint must not be empty")
        return fingerprint.strip()

    @staticmethod
    def _cert_subject(cert):
        subject = _cert_value(cert, "subject")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("certificate subject must not be empty")
        return subject.strip()

    @staticmethod
    def _cert_issuer(cert):
        issuer = _cert_value(cert, "issuer")
        if not isinstance(issuer, str) or not issuer.strip():
            raise ValueError("certificate issuer must not be empty")
        return issuer.strip()

    @staticmethod
    def _cert_not_before(cert):
        value = _cert_value(cert, "not_before", default=0)
        if value is None:
            return 0
        return value

    @staticmethod
    def _cert_not_after(cert):
        value = _cert_value(cert, "not_after", default=float("inf"))
        if value is None:
            return float("inf")
        return value

    def _cert_cn(self, cert):
        cn = _cert_value(cert, "cn", "common_name")
        if cn is None:
            subject = self._cert_subject(cert)
            match = re.search(r"(?:^|,)\s*CN=([^,]+)", subject, re.IGNORECASE)
            if match:
                cn = match.group(1)
        return cn

    def _is_self_signed(self, cert):
        explicit = _cert_value(cert, "self_signed")
        if explicit is not None:
            return bool(explicit)
        return self._cert_subject(cert) == self._cert_issuer(cert)
