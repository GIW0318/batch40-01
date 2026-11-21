import logging


logger = logging.getLogger(__name__)


DNS_CONTENT_TYPE = "application/dns-message"

RCODES = {
    0: "NOERROR",
    1: "FORMERR",
    2: "SERVFAIL",
    3: "NXDOMAIN",
    4: "NOTIMP",
    5: "REFUSED",
}

MAX_ATTEMPTS = 3


class DohResolverClient:
    """DNS-over-HTTPS client with bootstrap, pinning, padding and cache."""

    def __init__(self, bootstrap=("198.51.100.53",), server_name="doh.example",
                 pin_spki=None, pad_block=128, ttl_cap=300,
                 allow_plaintext=False, bootstrap_ttl=3600,
                 bootstrap_resolver=None, transport=None):
        self.bootstrap = list(bootstrap)
        self.server_name = server_name
        self.pin_spki = pin_spki
        self.pad_block = pad_block
        self.ttl_cap = ttl_cap
        self.allow_plaintext = allow_plaintext
        self.bootstrap_ttl = bootstrap_ttl
        self.bootstrap_resolver = bootstrap_resolver
        self.transport = transport
        self._cache = {}
        self._bootstrap_cached = None
        self._bootstrap_expires_at = 0
        self._stream_seq = 0
        self._connection_seq = 0
        self._stats = {
            "queries": 0,
            "cache_hits": 0,
            "errors": 0,
            "fallback": 0,
            "bootstrap_refreshes": 0,
        }

    def bootstrap_address(self, now=0):
        if self._bootstrap_cached is not None and now < self._bootstrap_expires_at:
            return self._bootstrap_cached
        if (self._bootstrap_cached is not None
                and now >= self._bootstrap_expires_at
                and self.bootstrap_resolver is not None):
            refreshed = self.bootstrap_resolver()
            if refreshed:
                self._stats["bootstrap_refreshes"] += 1
                logger.info(
                    "bootstrap address refreshed from configured resolver: %s",
                    refreshed[0],
                )
                self._bootstrap_cached = refreshed[0]
                self._bootstrap_expires_at = now + self.bootstrap_ttl
                return self._bootstrap_cached
        if self.bootstrap:
            self._bootstrap_cached = self.bootstrap[0]
            self._bootstrap_expires_at = now + self.bootstrap_ttl
        return self._bootstrap_cached

    def _dns_wire_length(self, name):
        labels = [label for label in name.split(".") if label]
        question = sum(len(label) + 1 for label in labels) + 1
        return 12 + question + 5

    def _padding_for(self, unpadded_length):
        block = self.pad_block or 0
        if block <= 1:
            return 0
        remainder = unpadded_length % block
        return (block - remainder) if remainder else block

    def _build_request(self, name, rtype, dnssec, cd, now, use_tcp=False,
                       transport_name="https"):
        unpadded_length = self._dns_wire_length(name)
        return {
            "address": self.bootstrap_address(now),
            "sni": self.server_name,
            "name": name,
            "rtype": rtype,
            "id": 0,
            "dnssec": dnssec,
            "cd": cd,
            "unpadded_length": unpadded_length,
            "padding": self._padding_for(unpadded_length),
            "stream_id": self._stream_seq,
            "connection": self._connection_seq,
            "tcp": use_tcp,
            "transport": transport_name,
        }

    def _cache_key(self, name, rtype, dnssec, cd):
        return (name.lower(), rtype.upper(), bool(dnssec), bool(cd))

    def _failure(self, error):
        self._stats["errors"] += 1
        return {"ok": False, "error": error}

    def resolve(self, name, rtype="A", dnssec=False, cd=False, now=0):
        self._stats["queries"] += 1
        key = self._cache_key(name, rtype, dnssec, cd)
        cached = self._cache.get(key)
        if cached is not None and now < cached["expires_at"]:
            self._stats["cache_hits"] += 1
            return cached["result"]

        self._stream_seq += 1
        request = self._build_request(name, rtype, dnssec, cd, now)
        attempts = 0
        use_tcp = False
        fallback_used = False

        while attempts < MAX_ATTEMPTS:
            attempts += 1
            try:
                response = self.transport(request)
            except Exception as exc:
                if not fallback_used and self.allow_plaintext:
                    fallback_used = True
                    self._stats["fallback"] += 1
                    logger.warning(
                        "DoH transport failed (%s); falling back to plaintext UDP",
                        exc,
                    )
                    request = self._build_request(
                        name, rtype, dnssec, cd, now, use_tcp=use_tcp,
                        transport_name="udp",
                    )
                    continue
                return self._failure("doh_failed")

            if (self.pin_spki is not None
                    and response["cert"].get("spki") != self.pin_spki):
                return self._failure("pin_mismatch")

            if response.get("content_type") != DNS_CONTENT_TYPE:
                return self._failure("bad_content_type")

            if response.get("h2_error"):
                logger.info(
                    "HTTP/2 error %s; retrying on a new connection",
                    response["h2_error"],
                )
                self._connection_seq += 1
                request = self._build_request(
                    name, rtype, dnssec, cd, now, use_tcp=use_tcp,
                    transport_name=request["transport"],
                )
                continue

            if response.get("truncated"):
                logger.info("truncated response; retrying over TCP")
                use_tcp = True
                request = self._build_request(
                    name, rtype, dnssec, cd, now, use_tcp=True,
                    transport_name=request["transport"],
                )
                continue

            rcode = response.get("rcode", 0)
            effective_ttl = min(response.get("ttl", 0), self.ttl_cap)
            result = {
                "ok": True,
                "name": name,
                "rtype": rtype,
                "rcode": rcode,
                "rcode_name": RCODES.get(rcode, "RCODE%d" % rcode),
                "answers": response.get("answers", []),
                "ttl": effective_ttl,
                "transport": request["transport"],
            }
            self._cache[key] = {
                "expires_at": now + effective_ttl,
                "result": result,
            }
            return result

        return self._failure("retry_exhausted")

    def stats(self):
        return dict(self._stats)
