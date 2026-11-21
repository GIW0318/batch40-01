"""Path policy for a self-service secrets store."""

import hashlib
import json
import re
import secrets
import time
import urllib.parse


_TEMPLATE_RE = re.compile(r"\$\{([^{}]+)\}")


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _expand_template(value, variables):
    if not isinstance(value, str):
        value = str(value)
    missing = False

    def replace(match):
        nonlocal missing
        name = match.group(1)
        if name not in variables:
            missing = True
            return ""
        return str(variables[name])

    expanded = _TEMPLATE_RE.sub(replace, value)
    if missing:
        return None
    return expanded


def _normalize_path(path):
    if not isinstance(path, str):
        if path is None:
            return None
        path = str(path)
    try:
        decoded = urllib.parse.unquote(
            path, encoding="utf-8", errors="strict"
        )
    except (UnicodeDecodeError, ValueError):
        return None

    segments = []
    for segment in decoded.replace("\\", "/").split("/"):
        if segment == "" or segment == ".":
            continue
        if segment == "..":
            if not segments:
                return None
            segments.pop()
            continue
        segments.append(segment)
    return "/".join(segments)


def _string_matches(left, right, case_sensitive):
    if not case_sensitive:
        left = left.casefold()
        right = right.casefold()
    return left == right


def _segment_matches(pattern, value, case_sensitive):
    if value == "":
        return False
    if not case_sensitive:
        pattern = pattern.casefold()
        value = value.casefold()
    if pattern == "*":
        return True
    if "*" not in pattern:
        return pattern == value
    expression = re.escape(pattern).replace(r"\*", ".*")
    return re.fullmatch(expression, value) is not None


def _path_matches(pattern, path, case_sensitive):
    if pattern is None or path is None:
        return False
    pattern_segments = pattern.split("/") if pattern else []
    path_segments = path.split("/") if path else []
    if len(pattern_segments) != len(path_segments):
        return False
    return all(
        _segment_matches(expected, actual, case_sensitive)
        for expected, actual in zip(pattern_segments, path_segments)
    )


class SecretsPathPolicy:
    """Normalize request paths and authorize read/list actions.

    ``rules`` is a list of ``{"effect": "allow"|"deny", "subjects": [...],
    "actions": [...], "path": "secret/data/${app}/*"}``.  A rule may use
    ``"subject"`` instead of ``"subjects"``.

    Requests are normalized before matching: backslashes become ``/``,
    percent escapes are decoded exactly once, empty and ``.`` segments are
    dropped and ``..`` is resolved; a path that would escape the root is
    rejected as ``bad_path``.  The empty path is the root and is refused with
    ``root_path`` unless a rule's normalized pattern is also empty.

    ``*`` matches exactly one non-empty segment and never crosses ``/``.
    There is no implicit subtree wildcard.  Matching is case-sensitive unless
    the policy was built with ``case_sensitive=False``, which lower-cases both
    sides.  ``${name}`` placeholders in subjects and paths are expanded from
    ``variables``; when a variable is missing the rule simply does not match
    (default deny).

    Deny rules are evaluated after every match and always beat allow rules,
    regardless of rule order.

    ``authorize(subject, action, path, lease_id=None, credential=None)``
    returns ``{"allowed", "reason", "path", "rule"}``.  ``rule`` is the index
    of the deciding rule or None.  Reasons are ``"allowed"``, ``"denied"``,
    ``"no_match"``, ``"root_path"``, ``"bad_path"`` and ``"lease_revoked"``.
    ``read`` and ``list`` are separate actions.

    ``issue_lease(subject, actions, paths, ttl=3600)`` returns a lease id,
    ``derive_credential(lease_id)`` returns a token bound to that lease and
    ``revoke_lease(lease_id)`` invalidates both the lease and every derived
    credential.  A request presenting a revoked lease or credential is denied
    with ``"lease_revoked"`` before rule matching.

    ``snapshot()`` returns ``{"rule_count", "digest", "case_sensitive"}`` and
    never the internal rule objects.
    """

    def __init__(self, rules, case_sensitive=True, variables=None):
        self.rules = list(rules)
        self.case_sensitive = case_sensitive
        self.variables = dict(variables or {})
        self._leases = {}
        self._credentials = {}

    def authorize(self, subject, action, path, lease_id=None, credential=None):
        normalized_path = _normalize_path(path)
        if normalized_path is None:
            return {
                "allowed": False,
                "reason": "bad_path",
                "path": path if isinstance(path, str) else "",
                "rule": None,
            }

        lease_reason = self._check_lease(
            subject, action, normalized_path, lease_id, credential
        )
        if lease_reason is not None:
            return {
                "allowed": False,
                "reason": lease_reason,
                "path": normalized_path,
                "rule": None,
            }

        compiled = []
        root_rule_exists = False
        for index, rule in enumerate(self.rules):
            candidate = self._compile_rule(rule)
            if candidate is None:
                continue
            compiled.append((index, candidate))
            if candidate["path"] == "":
                root_rule_exists = True

        if normalized_path == "" and not root_rule_exists:
            return {
                "allowed": False,
                "reason": "root_path",
                "path": normalized_path,
                "rule": None,
            }

        matches = []
        for index, rule in compiled:
            if not self._rule_matches(rule, subject, action, normalized_path):
                continue
            matches.append((index, rule["effect"]))

        for index, effect in matches:
            if effect == "deny":
                return {
                    "allowed": False,
                    "reason": "denied",
                    "path": normalized_path,
                    "rule": index,
                }

        for index, effect in matches:
            if effect == "allow":
                return {
                    "allowed": True,
                    "reason": "allowed",
                    "path": normalized_path,
                    "rule": index,
                }

        return {
            "allowed": False,
            "reason": "no_match",
            "path": normalized_path,
            "rule": None,
        }

    def issue_lease(self, subject, actions, paths, ttl=3600):
        lease_id = "lease_" + secrets.token_urlsafe(24)
        expires_at = None
        if ttl is not None:
            expires_at = time.time() + float(ttl)
        self._leases[lease_id] = {
            "subjects": [str(item) for item in _as_list(subject)],
            "actions": [str(item) for item in _as_list(actions)],
            "paths": [str(item) for item in _as_list(paths)],
            "expires_at": expires_at,
            "revoked": False,
        }
        return lease_id

    def derive_credential(self, lease_id):
        lease = self._leases.get(lease_id)
        if lease is None or lease["revoked"] or self._lease_expired(lease):
            raise ValueError("lease is not active")

        credential = "cred_" + secrets.token_urlsafe(32)
        self._credentials[credential] = lease_id
        return credential

    def revoke_lease(self, lease_id):
        lease = self._leases.get(lease_id)
        if lease is None:
            return False
        lease["revoked"] = True
        return True

    def snapshot(self):
        summaries = [self._rule_summary(rule) for rule in self.rules]
        variables = {str(key): value for key, value in self.variables.items()}
        payload = {
            "rules": summaries,
            "case_sensitive": self.case_sensitive,
            "variables": variables,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=repr,
        ).encode("utf-8")
        return {
            "rule_count": len(self.rules),
            "digest": hashlib.sha256(encoded).hexdigest(),
            "case_sensitive": self.case_sensitive,
        }

    def _compile_rule(self, rule):
        if not isinstance(rule, dict):
            return None

        effect = rule.get("effect")
        if isinstance(effect, str):
            effect = effect.lower()
        if effect not in ("allow", "deny"):
            return None

        raw_path = rule.get("path")
        if not isinstance(raw_path, str):
            return None
        expanded_path = _expand_template(raw_path, self.variables)
        if expanded_path is None:
            return None
        normalized_path = _normalize_path(expanded_path)
        if normalized_path is None:
            return None

        if "subjects" in rule:
            raw_subjects = _as_list(rule.get("subjects"))
        else:
            raw_subjects = _as_list(rule.get("subject"))
        subjects = []
        for item in raw_subjects:
            expanded = _expand_template(item, self.variables)
            if expanded is None:
                return None
            subjects.append(expanded)
        if not subjects:
            return None

        raw_actions = rule.get("actions", rule.get("action"))
        actions = [str(item) for item in _as_list(raw_actions)]
        if not actions:
            return None

        return {
            "effect": effect,
            "subjects": subjects,
            "actions": actions,
            "path": normalized_path,
        }

    def _rule_matches(self, rule, subject, action, path):
        if not any(
            _string_matches(candidate, str(subject), self.case_sensitive)
            for candidate in rule["subjects"]
        ):
            return False
        if not any(
            _string_matches(candidate, str(action), self.case_sensitive)
            for candidate in rule["actions"]
        ):
            return False
        return _path_matches(rule["path"], path, self.case_sensitive)

    def _check_lease(
        self, subject, action, path, lease_id=None, credential=None
    ):
        if lease_id is None and credential is None:
            return None

        if credential is not None:
            try:
                credential_lease = self._credentials.get(credential)
            except TypeError:
                return "lease_revoked"
            if credential_lease is None:
                return "lease_revoked"
            if lease_id is not None and credential_lease != lease_id:
                return "lease_revoked"
            lease_id = credential_lease

        try:
            lease = self._leases.get(lease_id)
        except TypeError:
            return "lease_revoked"
        if lease is None or lease["revoked"] or self._lease_expired(lease):
            return "lease_revoked"

        if lease["subjects"] and not any(
            _string_matches(candidate, str(subject), self.case_sensitive)
            for candidate in lease["subjects"]
        ):
            return "denied"
        if lease["actions"] and not any(
            _string_matches(candidate, str(action), self.case_sensitive)
            for candidate in lease["actions"]
        ):
            return "denied"
        if lease["paths"]:
            for pattern in lease["paths"]:
                expanded = _expand_template(pattern, self.variables)
                normalized = _normalize_path(expanded)
                if _path_matches(normalized, path, self.case_sensitive):
                    break
            else:
                return "denied"
        return None

    @staticmethod
    def _lease_expired(lease):
        expires_at = lease.get("expires_at")
        return expires_at is not None and time.time() > expires_at

    @staticmethod
    def _rule_summary(rule):
        if not isinstance(rule, dict):
            return {"rule": repr(rule)}
        if "subjects" in rule:
            subjects = _as_list(rule.get("subjects"))
        else:
            subjects = _as_list(rule.get("subject"))
        return {
            "effect": rule.get("effect"),
            "subjects": [str(item) for item in subjects],
            "actions": [
                str(item)
                for item in _as_list(
                    rule.get("actions", rule.get("action"))
                )
            ],
            "path": rule.get("path"),
        }
