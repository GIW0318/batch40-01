import copy
import json
import threading
import time


class StapleGate:
    """OCSP stapling verification gate with chain-wide checks and cache."""

    _NETWORK_ERROR_MARKERS = (
        "timeout",
        "timed out",
        "connection",
        "connect",
        "network",
        "dns",
        "socket",
        "unreachable",
        "refused",
        "reset",
        "temporar",
        "no route",
        "eof",
        "tls handshake",
        "ssl",
    )

    def __init__(self, allow_soft_fail=True, max_staple_size=16384,
                 clock_skew=300, cache_ttl=3600, fetch_source=None):
        self.allow_soft_fail = allow_soft_fail
        self.max_staple_size = max_staple_size
        self.clock_skew = clock_skew
        self.cache_ttl = cache_ttl
        self.fetch_source = fetch_source
        self._cache = {}
        self._lock = threading.RLock()

    def verify(self, chain, staples, nonce=None, now=0, fetch_error=None):
        chain = list(chain or [])
        staples = list(staples or [])
        assigned, unassigned = self._assign_staples(chain, staples)

        cert_results = []
        for index, cert in enumerate(chain):
            cert_results.append(
                self._verify_certificate(cert, assigned[index], nonce, now)
            )

        if unassigned:
            cert_results.append({
                "ok": False,
                "reason": "binding_mismatch",
                "cert_index": None,
            })

        failures = [result for result in cert_results if not result["ok"]]
        if not failures:
            return {
                "ok": True,
                "reason": "ok",
                "soft_failed": False,
                "cert_results": cert_results,
            }

        only_missing = all(
            result["reason"] == "must_staple_missing" for result in failures
        )
        if (only_missing and self.allow_soft_fail
                and self._is_network_error(fetch_error)):
            return {
                "ok": True,
                "reason": "soft_fail_network",
                "soft_failed": True,
                "cert_results": cert_results,
            }

        return {
            "ok": False,
            "reason": failures[0]["reason"],
            "soft_failed": False,
            "cert_results": cert_results,
        }

    def fetch(self, chain, now=0):
        if self.fetch_source is None:
            return self.cache(chain)

        response = self.fetch_source(chain)
        if response is None:
            return self.cache(chain)

        if isinstance(response, dict) and "staple" in response:
            staple = response["staple"]
            version = response.get("version", 0)
        else:
            staple = response
            version = 0

        self.store(chain, staple, version=version)
        return self.cache(chain)

    def store(self, chain, staple, version=0):
        key = self.cache_key(chain)
        now = time.monotonic()
        with self._lock:
            current = self._cache.get(key)
            if current is not None and current["version"] > version:
                return False
            self._cache[key] = {
                "version": version,
                "staple": copy.deepcopy(staple),
                "stored_at": now,
            }
            return True

    def cache(self, chain):
        key = self.cache_key(chain)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if (self.cache_ttl is not None
                    and self.cache_ttl >= 0
                    and time.monotonic() - entry["stored_at"] > self.cache_ttl):
                del self._cache[key]
                return None
            return copy.deepcopy(entry["staple"])

    def cache_key(self, chain):
        return json.dumps(
            list(chain or []),
            sort_keys=True,
            separators=(",", ":"),
            default=repr,
        )

    def policy(self):
        return {
            "allow_soft_fail": self.allow_soft_fail,
            "max_staple_size": self.max_staple_size,
            "clock_skew": self.clock_skew,
            "cache_ttl": self.cache_ttl,
            "require_nonce_match": True,
            "verify_full_chain": True,
        }

    def snapshot(self):
        with self._lock:
            entries = copy.deepcopy(self._cache)
        return {
            "policy": self.policy(),
            "cache": entries,
        }

    def _assign_staples(self, chain, staples):
        assigned = [None] * len(chain)
        unassigned = []

        for staple in staples:
            match = None
            for index, cert in enumerate(chain):
                if assigned[index] is None and self._binds(staple, cert):
                    match = index
                    break
            if match is None:
                unassigned.append(staple)
            else:
                assigned[match] = staple

        for staple in list(unassigned):
            for index in range(len(chain)):
                if assigned[index] is None:
                    assigned[index] = staple
                    unassigned.remove(staple)
                    break

        return assigned, unassigned

    def _verify_certificate(self, cert, staple, nonce, now):
        if staple is None:
            if cert.get("must_staple", False):
                return self._cert_failure(cert, "must_staple_missing")
            return self._cert_success(cert)

        if self._staple_size(staple) > self.max_staple_size:
            return self._cert_failure(cert, "staple_too_large")

        if not self._binds(staple, cert):
            return self._cert_failure(cert, "binding_mismatch")

        signer = staple.get("signer")
        allowed_signers = cert.get("authorized_signers") or ()
        signer_authorized = (
            signer == cert.get("issuer")
            or signer in allowed_signers
            or bool(staple.get("authorized", False))
        )
        if not signer_authorized:
            return self._cert_failure(cert, "unauthorized_signer")

        status = staple.get("status")
        if status == "revoked":
            return self._cert_failure(cert, "revoked")
        if status == "unknown":
            return self._cert_failure(cert, "unknown_status")
        if status != "good":
            return self._cert_failure(cert, "invalid_status")

        if nonce is not None and staple.get("nonce") != nonce:
            return self._cert_failure(cert, "nonce_mismatch")

        validity = self._validity_error(staple, now)
        if validity is not None:
            return self._cert_failure(cert, validity)

        return self._cert_success(cert)

    def _validity_error(self, staple, now):
        if "this_update" not in staple or "next_update" not in staple:
            return "invalid_validity"

        this_update = staple["this_update"]
        next_update = staple["next_update"]
        try:
            if this_update > now + self.clock_skew:
                return "not_yet_valid"
            if next_update < now - self.clock_skew:
                return "expired"
        except TypeError:
            return "invalid_validity"
        return None

    def _staple_size(self, staple):
        size = staple.get("size", 0)
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0

        body = staple.get("body")
        if body is not None:
            try:
                size = max(size, len(body))
            except TypeError:
                pass
        return size

    def _binds(self, staple, cert):
        serial_matches = staple.get("serial") == cert.get("serial")
        issuer_matches = (
            staple.get("issuer_key_hash") == cert.get("issuer_key_hash")
        )
        if "issuer" in staple:
            issuer_matches = (
                issuer_matches and staple["issuer"] == cert.get("issuer")
            )
        return serial_matches and issuer_matches

    def _is_network_error(self, fetch_error):
        if fetch_error is None:
            return False
        text = "{}:{}".format(
            type(fetch_error).__name__,
            str(fetch_error),
        ).lower()
        return any(marker in text for marker in self._NETWORK_ERROR_MARKERS)

    @staticmethod
    def _cert_success(cert):
        return {
            "ok": True,
            "reason": "ok",
            "subject": cert.get("subject"),
            "serial": cert.get("serial"),
        }

    @staticmethod
    def _cert_failure(cert, reason):
        return {
            "ok": False,
            "reason": reason,
            "subject": cert.get("subject"),
            "serial": cert.get("serial"),
        }
