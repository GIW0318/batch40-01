class ScopeAttenuationPlan:
    """Attenuate OAuth-style scopes on refresh and delegation."""

    def __init__(self, hierarchy=None, aliases=None, known_scopes=None,
                 max_scopes=32, delegations=None):
        self._hierarchy = {}
        for parent, children in (hierarchy or {}).items():
            self._hierarchy[self._clean(parent)] = [
                self._clean(child) for child in children
            ]
        self._aliases = {}
        for name, scopes in (aliases or {}).items():
            self._aliases[self._clean(name)] = [
                self._clean(scope) for scope in scopes
            ]
        self._known = (
            None if known_scopes is None
            else {self._clean(scope) for scope in known_scopes}
        )
        self._max_scopes = max_scopes
        self._user_grants = {}
        self._user_versions = {}
        self._counter = 0
        self._delegations = {}
        for (actor, user), scopes in (delegations or {}).items():
            self._delegations[(actor, user)] = set(self.normalize(scopes))

    @staticmethod
    def _clean(scope):
        return scope.strip().lower()

    def normalize(self, scopes):
        expanded = set()
        for raw in scopes or []:
            name = self._clean(raw)
            if not name:
                continue
            if name in self._aliases:
                expanded.update(self._aliases[name])
            else:
                expanded.add(name)
        if self._known is not None:
            unknown = expanded - self._known
            if unknown:
                raise ValueError(
                    "unknown scopes: %s" % ", ".join(sorted(unknown))
                )
        full = set(expanded)
        stack = list(expanded)
        while stack:
            current = stack.pop()
            for child in self._hierarchy.get(current, []):
                if child not in full:
                    full.add(child)
                    stack.append(child)
        if self._known is not None:
            unknown = full - self._known
            if unknown:
                raise ValueError(
                    "unknown scopes: %s" % ", ".join(sorted(unknown))
                )
        if len(full) > self._max_scopes:
            raise ValueError(
                "scope count %d exceeds max %d" % (len(full), self._max_scopes)
            )
        return sorted(full)

    def _normalize_audiences(self, audiences):
        return sorted({self._clean(aud) for aud in audiences or [] if self._clean(aud)})

    def _current_version(self, user):
        return self._user_versions.get(user, 0)

    def _require_current(self, token):
        if token["grant_version"] != self._current_version(token["user"]):
            raise ValueError("user scope grant changed; token is stale")

    def _make_token(self, user, scopes, audiences, actor_chain, version, now):
        self._counter += 1
        return {
            "token_id": "tok-%d" % self._counter,
            "user": user,
            "scopes": list(scopes),
            "audiences": list(audiences),
            "actor_chain": list(actor_chain),
            "grant_version": version,
            "issued_at": now,
        }

    def issue(self, user, scopes, audiences, actor=None, now=0):
        normalized = self.normalize(scopes)
        normalized_audiences = self._normalize_audiences(audiences)
        if not normalized_audiences:
            raise ValueError("at least one audience is required")
        actor_chain = []
        if actor is not None:
            grant = self._delegations.get((actor, user))
            if grant is None:
                raise ValueError("no delegation grant for actor %r" % actor)
            if not set(normalized) <= grant:
                raise ValueError("scopes exceed delegation grant")
            actor_chain = [actor]
        user_grant = self._user_grants.get(user)
        if user_grant is not None and not set(normalized) <= user_grant:
            raise ValueError("scopes exceed current user grant")
        return self._make_token(
            user, normalized, normalized_audiences, actor_chain,
            self._current_version(user), now,
        )

    def refresh(self, token, scopes=None, now=0):
        self._require_current(token)
        if scopes is None:
            new_scopes = list(token["scopes"])
        else:
            new_scopes = self.normalize(scopes)
            if not set(new_scopes) <= set(token["scopes"]):
                raise ValueError("refresh cannot widen scopes")
        return self._make_token(
            token["user"], new_scopes, token["audiences"],
            token["actor_chain"], self._current_version(token["user"]), now,
        )

    def attenuate(self, token, scopes, now=0):
        return self.refresh(token, scopes, now=now)

    def delegate(self, token, actor, scopes, audiences):
        self._require_current(token)
        grant = self._delegations.get((actor, token["user"]))
        if grant is None:
            raise ValueError("no delegation grant for actor %r" % actor)
        normalized = self.normalize(scopes)
        if not set(normalized) <= set(token["scopes"]):
            raise ValueError("delegation exceeds token scopes")
        if not set(normalized) <= grant:
            raise ValueError("delegation exceeds delegation grant")
        normalized_audiences = self._normalize_audiences(audiences)
        if not normalized_audiences or not set(normalized_audiences) <= set(token["audiences"]):
            raise ValueError("delegated audiences must be a subset of token audiences")
        return self._make_token(
            token["user"], normalized, normalized_audiences,
            token["actor_chain"] + [actor],
            self._current_version(token["user"]), 0,
        )

    def update_user_scopes(self, user, scopes, now=0):
        self._user_grants[user] = set(self.normalize(scopes))
        self._user_versions[user] = self._current_version(user) + 1

    def check(self, token, audience, now=0, scope=None):
        if token["grant_version"] != self._current_version(token["user"]):
            return {"allowed": False, "reason": "scope changed"}
        if self._clean(audience) not in token["audiences"]:
            return {"allowed": False, "reason": "audience mismatch"}
        if scope is not None:
            try:
                needed = self.normalize([scope])
            except ValueError:
                return {"allowed": False, "reason": "missing scope"}
            if not set(needed) <= set(token["scopes"]):
                return {"allowed": False, "reason": "missing scope"}
        return {"allowed": True, "reason": None}
