class SizeLimitExceeded(Exception):
    """Raised when the directory reports a truncated result set."""


def _split_unescaped(text, sep):
    parts, buf, i = [], [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            buf.append(ch)
            buf.append(text[i + 1])
            i += 2
            continue
        if ch == sep:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _referral_host(referral):
    if "://" not in referral:
        return None
    rest = referral.split("://", 1)[1]
    host = rest.split("/", 1)[0].split(":", 1)[0]
    return host.lower() or None


class LdapNestedGroupFold:
    """Expand nested LDAP groups into a transitive membership closure."""

    def __init__(self, source, follow_referrals=False, referral_allowlist=None,
                 max_depth=8, page_limit=100):
        self.source = source
        self.follow_referrals = follow_referrals
        self.referral_allowlist = {h.lower() for h in (referral_allowlist or [])}
        self.max_depth = max_depth
        self.page_limit = page_limit
        self._cache = {}

    def canonical_dn(self, dn):
        rdns = []
        for rdn in _split_unescaped(dn.strip(), ","):
            rdn = rdn.strip()
            if "=" in rdn:
                attr, _, value = rdn.partition("=")
                rdn = attr.strip() + "=" + value.strip()
            rdns.append(rdn.lower())
        return ",".join(rdns)

    def _load(self, cdn):
        members, referrals = [], []
        cursor, version, pages = None, None, 0
        while True:
            pages += 1
            if pages > self.page_limit:
                raise ValueError("pagination limit exceeded for %s" % cdn)
            page = self.source.fetch(cdn, cursor)
            if page.get("truncated"):
                raise SizeLimitExceeded("truncated result for %s" % cdn)
            if "members" not in page:
                raise ValueError("member attribute missing for %s" % cdn)
            members.extend(self.canonical_dn(m) for m in page["members"])
            referrals.extend(page.get("referrals") or [])
            version = page.get("version")
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        entry = (version, members, referrals)
        self._cache[cdn] = entry
        return entry

    def members(self, dn):
        _, members, _ = self._load(self.canonical_dn(dn))
        return sorted(members)

    def _expand(self, cdn, depth, path, out, memo):
        if depth > self.max_depth:
            raise ValueError("nesting depth limit exceeded at %s" % cdn)
        if cdn in memo:
            _, members, referrals = memo[cdn]
        else:
            try:
                _, members, referrals = self._load(cdn)
            except KeyError:
                if depth == 0:
                    raise
                return
            memo[cdn] = (_, members, referrals)
        for member in members:
            if self.source.is_group(member):
                if member in path:
                    raise ValueError("group cycle detected at %s" % member)
                self._expand(member, depth + 1, path + [member], out, memo)
            else:
                out.add(member)
        for referral in referrals:
            host = _referral_host(referral)
            if not (self.follow_referrals and host in self.referral_allowlist):
                continue
            if referral in path:
                raise ValueError("referral cycle detected at %s" % referral)
            self._expand(referral, depth + 1, path + [referral], out, memo)

    def effective_members(self, dn):
        cdn = self.canonical_dn(dn)
        out = set()
        self._expand(cdn, 0, [cdn], out, {})
        return out

    def groups_for(self, member_dn):
        cdn = self.canonical_dn(member_dn)
        found = set()
        for group_dn in self.source.list_groups():
            try:
                members = self.effective_members(group_dn)
            except KeyError:
                continue
            if cdn in members:
                found.add(self.canonical_dn(group_dn))
        return found

    def invalidate(self, dn):
        self._cache.pop(self.canonical_dn(dn), None)

    def refresh(self, dn=None):
        if dn is None:
            self._cache.clear()
            return
        cdn = self.canonical_dn(dn)
        self._cache.pop(cdn, None)
        self._load(cdn)
