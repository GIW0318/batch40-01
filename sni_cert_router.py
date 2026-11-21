import hashlib
import hmac
import os
import base64
import threading


TICKET_TTL_SECONDS = 300.0


class SniCertRouter:
    def __init__(self, default_domain=None):
        self.default_domain = default_domain
        self._lock = threading.RLock()
        self._version = 0
        self._snapshot = {
            "version": 0,
            "exact": {},
            "wildcard": {},
            "default": None,
        }
        self._ticket_secret = os.urandom(32)

    def normalize(self, name):
        if not isinstance(name, str):
            raise ValueError("SNI must be a string")
        if not name:
            raise ValueError("empty SNI")
        try:
            name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("SNI is not valid UTF-8") from exc
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
            raise ValueError("SNI contains illegal control characters")
        if name.endswith("."):
            name = name[:-1]
        if not name:
            raise ValueError("empty SNI")
        name = name.lower()
        labels = name.split(".")
        if any(label == "" for label in labels):
            raise ValueError("SNI contains an empty label")
        try:
            encoded_labels = [
                label.encode("idna").decode("ascii") for label in labels
            ]
        except UnicodeError as exc:
            raise ValueError("SNI is not a valid domain name") from exc
        return ".".join(encoded_labels)

    def _validate_wildcard_shape(self, normalized_domain):
        labels = normalized_domain.split(".")
        if len(labels) < 2 or labels[0] != "*" or "*" in labels[1:]:
            raise ValueError(
                "wildcard is only allowed as the leftmost label: %r"
                % normalized_domain
            )

    def load_batch(self, certs):
        exact = {}
        wildcard = {}
        default_cert = None

        for entry in certs:
            try:
                key = self.normalize(entry["domain"])
            except ValueError as exc:
                raise ValueError(
                    "invalid certificate domain %r: %s" % (entry.get("domain"), exc)
                ) from exc

            if "*" in key:
                self._validate_wildcard_shape(key)
                target = wildcard
            else:
                target = exact

            if key in exact or key in wildcard:
                raise ValueError("duplicate certificate for domain %r" % key)

            not_before = entry["not_before"]
            not_after = entry["not_after"]
            if not_before > not_after:
                raise ValueError(
                    "invalid validity window for %r: not_before=%s after not_after=%s"
                    % (key, not_before, not_after)
                )

            stored = dict(entry)
            stored["domain"] = key
            target[key] = stored

            if entry.get("default") and default_cert is None:
                default_cert = stored

        if default_cert is None and self.default_domain is not None:
            try:
                default_key = self.normalize(self.default_domain)
            except ValueError as exc:
                raise ValueError("invalid default domain") from exc
            default_cert = exact.get(default_key)

        with self._lock:
            self._version += 1
            new_snapshot = {
                "version": self._version,
                "exact": exact,
                "wildcard": wildcard,
                "default": default_cert,
            }
            self._snapshot = new_snapshot
            return self._version

    def _negotiate_alpn(self, cert_entry, client_alpn):
        server_alpn = cert_entry.get("alpn") or []
        if not client_alpn:
            return None
        for protocol in server_alpn:
            if protocol in client_alpn:
                return protocol
        raise ValueError("no overlapping ALPN protocol")

    def select(self, sni, alpn=None, now=0.0):
        with self._lock:
            snapshot = self._snapshot

        if sni is None:
            cert_entry = snapshot["default"]
            if cert_entry is None:
                raise ValueError("no SNI provided and no default certificate configured")
        else:
            key = self.normalize(sni)
            cert_entry = snapshot["exact"].get(key)
            if cert_entry is None:
                parent = key.split(".", 1)[1] if "." in key else ""
                cert_entry = snapshot["wildcard"].get("*." + parent) if parent else None
            if cert_entry is None:
                raise ValueError("no certificate matches SNI %r" % key)

        if now < cert_entry["not_before"] or now > cert_entry["not_after"]:
            raise ValueError(
                "certificate %r is not valid at %s" % (cert_entry["cert_id"], now)
            )

        result = dict(cert_entry)
        result["alpn"] = self._negotiate_alpn(cert_entry, alpn)
        result["version"] = snapshot["version"]
        return result

    def issue_ticket(self, domain, now):
        key = self.normalize(domain)
        issued_at = float(now)
        payload = key.encode("utf-8") + b"|" + repr(issued_at).encode("ascii")
        signature = base64.urlsafe_b64encode(
            hmac.new(self._ticket_secret, payload, hashlib.sha256).digest()
        )
        ticket = payload + b"." + signature
        return {
            "ticket": ticket,
            "domain": key,
            "issued_at": issued_at,
            "ttl": TICKET_TTL_SECONDS,
        }

    def resume_ticket(self, ticket, sni, now):
        if isinstance(ticket, str):
            ticket = ticket.encode("latin-1")
        if not isinstance(ticket, (bytes, bytearray)) or b"." not in ticket:
            return False

        payload, _, signature_b64 = ticket.rpartition(b".")
        try:
            signature = base64.urlsafe_b64decode(signature_b64)
        except (ValueError, TypeError):
            return False
        expected = hmac.new(
            self._ticket_secret, payload, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            return False
        if b"|" not in payload:
            return False
        domain_bytes, _, issued_bytes = payload.rpartition(b"|")

        try:
            ticket_domain = domain_bytes.decode("utf-8")
            issued_at = float(issued_bytes.decode("ascii"))
            sni_domain = self.normalize(sni)
        except (ValueError, UnicodeDecodeError):
            return False

        if not hmac.compare_digest(ticket_domain, sni_domain):
            return False
        if float(now) - issued_at > TICKET_TTL_SECONDS:
            return False
        return True

    def snapshot(self):
        with self._lock:
            return self._snapshot
