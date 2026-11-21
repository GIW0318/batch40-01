import hashlib
import hmac
import secrets
import threading

DEFAULT_TARGET = {
    "algo": "argon2id",
    "params": {"m": 1024, "t": 2, "p": 1},
    "version": 2,
}

DEFAULT_PEPPERS = {
    1: "pepper-secret-v1",
    2: "pepper-secret-v2",
}

DEFAULT_CURRENT_PEPPER = 2

_DEFAULT_PARAMS = {
    "argon2id": {"m": 1024, "t": 2, "p": 1},
    "bcrypt": {"cost": 12},
}


def _canonical_params(params):
    return ",".join("{}={}".format(k, params[k]) for k in sorted(params))


def _digest(algo, params, salt, pepper_secret, password):
    material = "{}|{}|{}|{}|{}".format(
        algo, _canonical_params(params), salt, pepper_secret, password
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class VerifierRehashPlan:
    """Lazy password hash upgrade planning on the successful login path."""

    def __init__(self, target=None, peppers=None, current_pepper=None,
                 max_password_bytes=1024):
        self.target = dict(target or DEFAULT_TARGET)
        self.target["params"] = dict(self.target["params"])
        self.peppers = dict(peppers or DEFAULT_PEPPERS)
        self.current_pepper = (
            current_pepper if current_pepper is not None
            else DEFAULT_CURRENT_PEPPER
        )
        self.max_password_bytes = max_password_bytes
        self._records = {}
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._queue = {}
        self._stats = {
            "verifications": 0,
            "fake_verifications": 0,
            "rehashes": 0,
            "corrupt": 0,
        }

    @staticmethod
    def hash_password(password, algo="argon2id", params=None, pepper_version=1,
                      version=1, salt=None, pepper_secret=None):
        if params is None:
            params = dict(_DEFAULT_PARAMS.get(algo, {}))
        else:
            params = dict(params)
        if salt is None:
            salt = secrets.token_hex(8)
        if pepper_secret is None:
            pepper_secret = DEFAULT_PEPPERS.get(
                pepper_version, "unknown-pepper-{}".format(pepper_version)
            )
        digest = _digest(algo, params, salt, pepper_secret, password)
        return "v{}${}${}${}${}${}".format(
            version, algo, _canonical_params(params), salt, pepper_version,
            digest,
        )

    def parse(self, stored):
        if not isinstance(stored, str):
            raise ValueError("stored hash must be a string")
        parts = stored.split("$")
        if len(parts) != 6:
            raise ValueError("malformed stored hash")
        version_tag, algo, params_str, salt, pepper_tag, digest = parts
        if not version_tag.startswith("v"):
            raise ValueError("missing version prefix")
        try:
            version = int(version_tag[1:])
            pepper_version = int(pepper_tag)
        except ValueError:
            raise ValueError("malformed version fields")
        if not algo or not salt or not digest:
            raise ValueError("missing hash fields")
        params = {}
        if params_str:
            for pair in params_str.split(","):
                key, sep, value = pair.partition("=")
                if not sep or not key:
                    raise ValueError("malformed parameters")
                try:
                    params[key] = int(value)
                except ValueError:
                    raise ValueError("non-numeric parameter value")
        return {
            "version": version,
            "algo": algo,
            "params": params,
            "salt": salt,
            "pepper": pepper_version,
            "digest": digest,
        }

    def register(self, user, stored):
        self._records[user] = stored

    def record(self, user):
        return self._records.get(user)

    def _lock_for(self, user):
        with self._locks_guard:
            return self._locks.setdefault(user, threading.Lock())

    def _verify(self, parsed, password):
        pepper_secret = self.peppers.get(parsed["pepper"])
        if pepper_secret is None:
            return False
        candidate = _digest(
            parsed["algo"], parsed["params"], parsed["salt"],
            pepper_secret, password,
        )
        return hmac.compare_digest(candidate, parsed["digest"])

    def _needs_rehash(self, parsed):
        if parsed["version"] < self.target["version"]:
            return True
        if parsed["algo"] != self.target["algo"]:
            return True
        if parsed["pepper"] != self.current_pepper:
            return True
        for key, target_value in self.target["params"].items():
            if parsed["params"].get(key, 0) < target_value:
                return True
        return False

    def _rehash(self, user, password):
        with self._lock_for(user):
            try:
                parsed = self.parse(self._records[user])
            except ValueError:
                return False
            if not self._needs_rehash(parsed):
                return False
            upgraded = self.hash_password(
                password,
                algo=self.target["algo"],
                params=self.target["params"],
                pepper_version=self.current_pepper,
                version=self.target["version"],
                salt=secrets.token_hex(8),
                pepper_secret=self.peppers.get(self.current_pepper),
            )
            self._records[user] = upgraded
            self._stats["rehashes"] += 1
            if user not in self._queue:
                self._queue[user] = {
                    "user": user,
                    "from": {
                        "algo": parsed["algo"],
                        "params": dict(parsed["params"]),
                        "version": parsed["version"],
                    },
                    "to": {
                        "algo": self.target["algo"],
                        "params": dict(self.target["params"]),
                        "version": self.target["version"],
                    },
                }
            return True

    def login(self, user, password, now=0):
        if len(password.encode("utf-8")) > self.max_password_bytes:
            return {
                "ok": False,
                "reason": "password too long",
                "rehashed": False,
            }
        stored = self._records.get(user)
        if stored is None:
            self._stats["fake_verifications"] += 1
            self.hash_password(
                password,
                algo=self.target["algo"],
                params=self.target["params"],
                pepper_version=self.current_pepper,
                version=self.target["version"],
                salt="fake-salt",
                pepper_secret=self.peppers.get(self.current_pepper),
            )
            return {
                "ok": False,
                "reason": "invalid credentials",
                "rehashed": False,
            }
        try:
            parsed = self.parse(stored)
        except ValueError:
            self._stats["corrupt"] += 1
            return {
                "ok": False,
                "reason": "reset required",
                "rehashed": False,
            }
        if parsed["version"] > self.target["version"]:
            return {
                "ok": False,
                "reason": "unsupported version",
                "rehashed": False,
            }
        self._stats["verifications"] += 1
        if not self._verify(parsed, password):
            return {
                "ok": False,
                "reason": "invalid credentials",
                "rehashed": False,
            }
        rehashed = False
        if self._needs_rehash(parsed):
            rehashed = self._rehash(user, password)
        return {"ok": True, "rehashed": rehashed, "pending": self.pending()}

    def pending(self):
        return len(self._queue)

    def plan(self):
        return [dict(task) for task in self._queue.values()]

    def drain(self):
        tasks = self.plan()
        self._queue.clear()
        return tasks

    def report(self):
        return {
            "current": {
                "algo": self.target["algo"],
                "params": dict(self.target["params"]),
                "version": self.target["version"],
            },
            "target": {
                "algo": self.target["algo"],
                "params": dict(self.target["params"]),
                "version": self.target["version"],
            },
            "pending": self.pending(),
        }

    def stats(self):
        return dict(self._stats)
