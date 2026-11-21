import copy
import re
from urllib.parse import unquote


class VersionConflict(Exception):
    """Raised when a SCIM resource is updated with a stale version."""


class ScimDeprovisionSync:
    """SCIM 2.0 user/group sync with deprovision propagation.

    The implementation deliberately keeps deprovisioning as a state machine:
    deactivating or deleting a user also removes every authorization path
    (nested groups, roles) and every credential (sessions, tokens).
    """

    _SCHEMAS = ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    _BULK_SCHEMAS = ["urn:ietf:params:scim:api:messages:2.0:BulkResponse"]
    _FILTER_RE = re.compile(
        r'^\s*([A-Za-z0-9_.:$-]+)\s+(eq|ne|co|sw|ew)\s+"((?:[^"\\]|\\.)*)"\s*$',
        re.IGNORECASE,
    )
    _MEMBER_FILTER_RE = re.compile(
        r'^\s*members\s*\[\s*value\s+eq\s+"([^"]+)"\s*\]\s*$',
        re.IGNORECASE,
    )

    def __init__(self, page_size=2):
        if not isinstance(page_size, int) or page_size < 1:
            raise ValueError("page_size must be a positive integer")
        self.page_size = page_size
        self.users = {}
        self.groups = {}
        self._sessions = {}
        self._tokens = {}
        self._next_user_id = 1
        self._next_group_id = 1
        self._next_session_id = 1
        self._next_token_id = 1

        # Tombstones are intentionally kept forever. A replay of a create
        # from the HR source must not bring a deleted identity back to life.
        self._deleted_user_ids = set()
        self._deleted_user_names = set()
        self._deleted_external_ids = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clone(value):
        return copy.deepcopy(value)

    @staticmethod
    def _canonical(value):
        return str(value).casefold()

    @staticmethod
    def _next_version(resource):
        try:
            return str(int(resource.get("version", "0")) + 1)
        except (TypeError, ValueError):
            return "1"

    @staticmethod
    def _check_version(resource, version):
        if version is None:
            return
        if str(version) != str(resource.get("version")):
            raise VersionConflict(
                "stale version %s for resource %s (current %s)"
                % (version, resource.get("id"), resource.get("version"))
            )

    def _get_user(self, user_id):
        try:
            return self.users[user_id]
        except KeyError:
            raise KeyError(user_id)

    def _get_group(self, group_id):
        try:
            return self.groups[group_id]
        except KeyError:
            raise KeyError(group_id)

    def _name_is_available(self, user_name, exclude_id=None):
        canonical = self._canonical(user_name)
        if canonical in self._deleted_user_names:
            return False
        for user_id, user in self.users.items():
            if user_id != exclude_id and self._canonical(user.get("userName")) == canonical:
                return False
        return True

    def _external_id_is_available(self, external_id, exclude_id=None):
        if external_id is None or external_id == "":
            return True
        canonical = self._canonical(external_id)
        if canonical in self._deleted_external_ids:
            return False
        for user_id, user in self.users.items():
            current = user.get("externalId")
            if current is None or current == "":
                continue
            if user_id != exclude_id and self._canonical(current) == canonical:
                return False
        return True

    def _validate_identity(self, user_name, external_id, exclude_id=None):
        if not isinstance(user_name, str) or not user_name:
            raise ValueError("userName is required")
        if not self._name_is_available(user_name, exclude_id):
            raise ValueError("userName already exists")
        if not self._external_id_is_available(external_id, exclude_id):
            raise ValueError("externalId already exists")

    def _touch_ancestors(self, group_id):
        """Bump versions on ancestor groups after effective membership changes."""
        seen = set()
        current = self.groups.get(group_id)
        while current is not None:
            parent_id = current.get("parent")
            if not parent_id or parent_id in seen:
                break
            seen.add(parent_id)
            parent = self.groups.get(parent_id)
            if parent is None:
                break
            parent["version"] = self._next_version(parent)
            current = parent

    def _remove_direct_membership(self, user_id):
        changed = False
        for group in self.groups.values():
            members = group.setdefault("members", [])
            if user_id in members:
                group["members"] = [member for member in members if member != user_id]
                group["version"] = self._next_version(group)
                self._touch_ancestors(group["id"])
                changed = True
        return changed

    def _deprovision_state(self, user):
        """Remove all authorization and credential state for a user."""
        user_id = user["id"]
        changed = False

        if user.get("roles"):
            user["roles"] = []
            changed = True

        if self._remove_direct_membership(user_id):
            changed = True

        if self._sessions.pop(user_id, None):
            changed = True
        if self._tokens.pop(user_id, None):
            changed = True

        return changed

    def _commit_user(self, user, proposed):
        """Apply a proposed user resource and propagate deprovision state."""
        previous = self._clone(user)
        user.clear()
        user.update(proposed)

        propagated = False
        if user.get("active") is False:
            propagated = self._deprovision_state(user)

        if user != previous or propagated:
            user["version"] = self._next_version(previous)
            return True
        return False

    @staticmethod
    def _to_bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
        raise ValueError("active must be a boolean")

    def _attribute_values(self, user, attribute):
        attribute = attribute.strip().casefold()
        if ":" in attribute:
            attribute = attribute.rsplit(":", 1)[-1]

        if attribute == "id":
            return [user.get("id")]
        if attribute == "username":
            return [user.get("userName")]
        if attribute == "externalid":
            value = user.get("externalId")
            return [] if value is None else [value]
        if attribute == "active":
            return [user.get("active")]

        if attribute in ("emails", "emails.value", "email"):
            values = []
            for email in user.get("emails", []) or []:
                if isinstance(email, dict):
                    value = email.get("value")
                else:
                    value = email
                if value is not None:
                    values.append(value)
            return values

        if attribute in ("roles", "roles.value"):
            values = []
            for role in user.get("roles", []) or []:
                if isinstance(role, dict):
                    value = role.get("value")
                else:
                    value = role
                if value is not None:
                    values.append(value)
            return values

        if attribute.startswith("name."):
            name = user.get("name") or {}
            if isinstance(name, dict):
                value = name.get(attribute.split(".", 1)[1])
                return [] if value is None else [value]
            return []

        value = user.get(attribute)
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def _matches_filter(self, user, attribute, operator, expected):
        values = self._attribute_values(user, attribute)
        expected_cf = self._canonical(expected)

        def equal(value):
            if isinstance(value, bool):
                return value == (expected_cf in ("true", "1"))
            return self._canonical(value) == expected_cf

        if operator in ("eq", "ne"):
            matched = any(equal(value) for value in values)
            return matched if operator == "eq" else not matched

        for value in values:
            actual_cf = self._canonical(value)
            if operator == "co" and expected_cf in actual_cf:
                return True
            if operator == "sw" and actual_cf.startswith(expected_cf):
                return True
            if operator == "ew" and actual_cf.endswith(expected_cf):
                return True
        return False

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def create_user(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("user payload must be an object")

        user_name = payload.get("userName")
        external_id = payload.get("externalId")
        self._validate_identity(user_name, external_id)

        user_id = "user-%d" % self._next_user_id
        self._next_user_id += 1

        user = self._clone(payload)
        user.pop("id", None)
        user.pop("version", None)
        user.pop("meta", None)
        user["id"] = user_id
        user["userName"] = user_name
        user["active"] = self._to_bool(payload.get("active", True))
        user["version"] = "1"
        user.setdefault("emails", [])
        user.setdefault("roles", [])
        if external_id is not None:
            user["externalId"] = external_id

        self.users[user_id] = user
        return self._clone(user)

    def get_user(self, user_id):
        return self._clone(self._get_user(user_id))

    def find_user(self, filter_expr):
        if not isinstance(filter_expr, str):
            raise ValueError("filter must be a string")
        match = self._FILTER_RE.match(filter_expr)
        if match is None:
            raise ValueError("unsupported filter expression")

        attribute, operator, expected = match.groups()
        expected = expected.replace('\\"', '"').replace("\\\\", "\\")
        operator = operator.casefold()
        for user in self.users.values():
            if self._matches_filter(user, attribute, operator, expected):
                return self._clone(user)
        return None

    def list_users(self, start_index=1, count=None):
        if not isinstance(start_index, int) or start_index < 1:
            raise ValueError("startIndex is one-based")
        if count is None:
            count = self.page_size
        if not isinstance(count, int) or count < 0:
            raise ValueError("count must not be negative")

        ordered = list(self.users.values())
        offset = start_index - 1
        if count == 0:
            resources = []
        else:
            resources = ordered[offset:offset + count]

        return {
            "schemas": self._clone(self._SCHEMAS),
            "totalResults": len(ordered),
            "startIndex": start_index,
            "itemsPerPage": len(resources),
            "Resources": self._clone(resources),
        }

    def update_user(self, user_id, payload, version=None):
        user = self._get_user(user_id)
        self._check_version(user, version)
        if not isinstance(payload, dict):
            raise ValueError("user payload must be an object")

        proposed = self._clone(user)
        for key, value in payload.items():
            if key in ("id", "version", "meta"):
                continue
            proposed[key] = self._clone(value)

        self._validate_identity(
            proposed.get("userName"), proposed.get("externalId"), exclude_id=user_id
        )
        if "active" in payload:
            proposed["active"] = self._to_bool(payload["active"])

        self._commit_user(user, proposed)
        return self._clone(user)

    def _validate_patch_operation(self, operation):
        if not isinstance(operation, dict):
            raise ValueError("patch operation must be an object")
        op = str(operation.get("op", "")).casefold()
        if op not in ("add", "replace", "remove"):
            raise ValueError("unsupported patch operation")
        path = operation.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("patch path is required")

        normalized = path.strip().casefold()
        allowed = {
            "active",
            "username",
            "externalid",
            "emails",
            "roles",
            "name",
        }
        if normalized not in allowed and not normalized.startswith("name."):
            raise ValueError("unsupported patch path: %s" % path)

        if op == "remove" and normalized in ("active",):
            raise ValueError("active cannot be removed")
        return op, normalized

    def _apply_user_operation(self, user, operation):
        op, path = self._validate_patch_operation(operation)
        value = operation.get("value")

        if path == "active":
            if op == "remove":
                raise ValueError("active cannot be removed")
            user["active"] = self._to_bool(value)
        elif path == "username":
            if op == "remove" or not isinstance(value, str) or not value:
                raise ValueError("userName cannot be removed")
            user["userName"] = value
        elif path == "externalid":
            if op == "remove":
                user.pop("externalId", None)
            elif value is None or value == "":
                user.pop("externalId", None)
            else:
                user["externalId"] = value
        elif path == "emails":
            if op == "add":
                current = list(user.get("emails") or [])
                additions = value if isinstance(value, list) else [value]
                current.extend(self._clone(additions))
                user["emails"] = current
            elif op == "remove":
                user["emails"] = []
            else:
                user["emails"] = self._clone(value if isinstance(value, list) else [value])
        elif path == "roles":
            if op == "add":
                current = list(user.get("roles") or [])
                additions = value if isinstance(value, list) else [value]
                current.extend(self._clone(additions))
                user["roles"] = current
            elif op == "remove":
                user["roles"] = []
            else:
                user["roles"] = self._clone(value if isinstance(value, list) else [value])
        elif path == "name":
            if op == "remove":
                user.pop("name", None)
            else:
                user["name"] = self._clone(value)
        elif path.startswith("name."):
            attribute = path.split(".", 1)[1]
            name = self._clone(user.get("name") or {})
            if not isinstance(name, dict):
                name = {}
            if op == "remove":
                name.pop(attribute, None)
            else:
                name[attribute] = self._clone(value)
            user["name"] = name

    def patch_user(self, user_id, operations, version=None):
        user = self._get_user(user_id)
        self._check_version(user, version)
        if not isinstance(operations, list):
            raise ValueError("operations must be a list")

        # Validate the complete patch before changing anything. A silently
        # skipped operation is exactly how the production incident happened.
        for operation in operations:
            self._validate_patch_operation(operation)

        proposed = self._clone(user)
        for operation in operations:
            self._apply_user_operation(proposed, operation)

        self._validate_identity(
            proposed.get("userName"), proposed.get("externalId"), exclude_id=user_id
        )
        self._commit_user(user, proposed)
        return self._clone(user)

    def deactivate_user(self, user_id):
        user = self._get_user(user_id)
        if user.get("active") is False:
            # Still repair any stale credentials/memberships left by an older
            # implementation, but keep the version stable for idempotency.
            self._deprovision_state(user)
            return self._clone(user)

        user["active"] = False
        self._deprovision_state(user)
        user["version"] = self._next_version(user)
        return self._clone(user)

    def delete_user(self, user_id):
        if user_id in self._deleted_user_ids:
            return {"deleted": True, "id": user_id}

        user = self._get_user(user_id)
        self._deleted_user_ids.add(user_id)
        self._deleted_user_names.add(self._canonical(user.get("userName")))
        external_id = user.get("externalId")
        if external_id is not None and external_id != "":
            self._deleted_external_ids.add(self._canonical(external_id))

        self._deprovision_state(user)
        self._sessions.pop(user_id, None)
        self._tokens.pop(user_id, None)
        del self.users[user_id]
        return {"deleted": True, "id": user_id}

    # ------------------------------------------------------------------
    # Groups and nested membership
    # ------------------------------------------------------------------

    def create_group(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("group payload must be an object")
        display_name = payload.get("displayName")
        if not isinstance(display_name, str) or not display_name:
            raise ValueError("displayName is required")

        parent = payload.get("parent")
        if parent is not None:
            self._get_group(parent)

        group_id = "group-%d" % self._next_group_id
        self._next_group_id += 1

        group = self._clone(payload)
        group["id"] = group_id
        group["displayName"] = display_name
        group["version"] = "1"
        if parent is not None:
            group["parent"] = parent
        else:
            group.pop("parent", None)
        group["members"] = self._normalize_members(payload.get("members"))
        self._validate_group_members(group["members"])
        group.setdefault("roles", [])
        self.groups[group_id] = group
        return self._clone(group)

    @staticmethod
    def _normalize_members(members):
        result = []
        if not members:
            return result
        if not isinstance(members, list):
            raise ValueError("members must be a list")
        for member in members:
            if isinstance(member, dict):
                member = member.get("value") or member.get("id")
            if not isinstance(member, str) or not member:
                raise ValueError("invalid group member")
            if member not in result:
                result.append(member)
        return result

    def _validate_group_members(self, members):
        for member in members:
            user = self.users.get(member)
            if user is None:
                raise ValueError("group member does not exist: %s" % member)
            if user.get("active") is False:
                raise ValueError("cannot add an inactive user to a group")

    def add_member(self, group_id, user_id):
        group = self._get_group(group_id)
        user = self._get_user(user_id)
        if user.get("active") is False:
            raise ValueError("cannot add an inactive user to a group")

        members = group.setdefault("members", [])
        if user_id not in members:
            members.append(user_id)
            group["version"] = self._next_version(group)
            self._touch_ancestors(group_id)
        return self._clone(group)

    def remove_member(self, group_id, user_id):
        group = self._get_group(group_id)
        members = group.setdefault("members", [])
        if user_id in members:
            group["members"] = [member for member in members if member != user_id]
            group["version"] = self._next_version(group)
            self._touch_ancestors(group_id)
        return self._clone(group)

    def _validate_group_operation(self, operation):
        if not isinstance(operation, dict):
            raise ValueError("patch operation must be an object")
        op = str(operation.get("op", "")).casefold()
        if op not in ("add", "replace", "remove"):
            raise ValueError("unsupported patch operation")
        path = operation.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("patch path is required")
        normalized = path.strip()

        member_filter = self._MEMBER_FILTER_RE.match(normalized)
        if member_filter is not None:
            if op != "remove":
                raise ValueError("member filters are only supported for remove")
            return op, "member-filter", member_filter.group(1)

        lowered = normalized.casefold()
        if lowered == "members":
            return op, "members", None
        if lowered == "displayname":
            if op == "remove":
                raise ValueError("displayName cannot be removed")
            return op, "displayname", None
        if lowered == "parent":
            return op, "parent", None

        # Anything with a filter we do not understand must be rejected, not
        # silently ignored.
        if "[" in lowered:
            raise ValueError("unsupported patch filter: %s" % path)
        raise ValueError("unsupported patch path: %s" % path)

    def patch_group(self, group_id, operations, version=None):
        group = self._get_group(group_id)
        self._check_version(group, version)
        if not isinstance(operations, list):
            raise ValueError("operations must be a list")

        for operation in operations:
            self._validate_group_operation(operation)

        proposed = self._clone(group)
        membership_changed = False
        for operation in operations:
            op, kind, filter_value = self._validate_group_operation(operation)
            value = operation.get("value")

            if kind == "member-filter":
                changed, proposed["members"] = self._remove_filtered_member(
                    proposed.get("members", []), filter_value
                )
                membership_changed = membership_changed or changed
            elif kind == "members":
                if op == "remove":
                    new_members = []
                else:
                    new_members = self._normalize_members(value)
                self._validate_group_members(new_members)
                if new_members != proposed.get("members", []):
                    membership_changed = True
                proposed["members"] = new_members
            elif kind == "displayname":
                proposed["displayName"] = value
            elif kind == "parent":
                if op == "remove" or value is None:
                    proposed.pop("parent", None)
                else:
                    self._get_group(value)
                    proposed["parent"] = value

        if proposed.get("parent") == group_id:
            raise ValueError("a group cannot be its own parent")

        changed = proposed != group
        if changed:
            group.clear()
            group.update(proposed)
            group["version"] = self._next_version(proposed)
            if membership_changed:
                self._touch_ancestors(group_id)
        return self._clone(group)

    @staticmethod
    def _remove_filtered_member(members, user_id):
        if user_id in members:
            return True, [member for member in members if member != user_id]
        return False, list(members)

    def effective_groups(self, user_id):
        user = self.users.get(user_id)
        if user is None or user.get("active") is False:
            return []

        result = set()
        for group_id, group in self.groups.items():
            if user_id not in group.get("members", []):
                continue

            current_id = group_id
            visited = set()
            while current_id and current_id not in visited:
                visited.add(current_id)
                result.add(current_id)
                current = self.groups.get(current_id)
                if current is None:
                    break
                current_id = current.get("parent")

        return sorted(result)

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def create_session(self, user_id, now=0):
        user = self._get_user(user_id)
        if user.get("active") is False:
            raise ValueError("cannot create a session for an inactive user")

        session = {
            "id": "session-%d" % self._next_session_id,
            "userId": user_id,
            "createdAt": now,
        }
        self._next_session_id += 1
        self._sessions.setdefault(user_id, []).append(session)
        return self._clone(session)

    def create_token(self, user_id, now=0):
        """Issue a credential; it is revoked with its owning user."""
        user = self._get_user(user_id)
        if user.get("active") is False:
            raise ValueError("cannot create a token for an inactive user")

        token = {
            "id": "token-%d" % self._next_token_id,
            "userId": user_id,
            "createdAt": now,
        }
        self._next_token_id += 1
        self._tokens.setdefault(user_id, []).append(token)
        return self._clone(token)

    def sessions(self, user_id):
        return self._clone(self._sessions.get(user_id, []))

    def tokens(self, user_id):
        return self._clone(self._tokens.get(user_id, []))

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def _bulk_execute(self, operation):
        method = str(operation.get("method", "")).upper()
        raw_path = operation.get("path")
        if not isinstance(raw_path, str):
            raise ValueError("operation path is required")
        path = raw_path.split("?", 1)[0].rstrip("/")
        parts = [unquote(part) for part in path.split("/") if part]

        if len(parts) == 1 and parts[0].casefold() == "users":
            if method == "POST":
                return "201", self.create_user(operation.get("data"))
            raise ValueError("unsupported bulk operation")

        if len(parts) == 2 and parts[0].casefold() == "users":
            user_id = parts[1]
            if method == "GET":
                return "200", self.get_user(user_id)
            if method == "DELETE":
                return "204", self.delete_user(user_id)
            if method in ("PUT", "PATCH"):
                data = operation.get("data")
                version = operation.get("version")
                if method == "PUT":
                    return "200", self.update_user(user_id, data, version=version)
                return "200", self.patch_user(user_id, data, version=version)
            raise ValueError("unsupported bulk operation")

        if len(parts) == 1 and parts[0].casefold() == "groups":
            if method == "POST":
                return "201", self.create_group(operation.get("data"))
            raise ValueError("unsupported bulk operation")

        if len(parts) == 2 and parts[0].casefold() == "groups":
            group_id = parts[1]
            if method == "GET":
                return "200", self._clone(self._get_group(group_id))
            if method in ("PUT", "PATCH"):
                return "200", self.patch_group(
                    group_id, operation.get("data"), version=operation.get("version")
                )
            raise ValueError("unsupported bulk operation")

        raise KeyError(raw_path)

    def bulk(self, operations):
        if not isinstance(operations, list):
            raise ValueError("operations must be a list")

        results = []
        for operation in operations:
            if not isinstance(operation, dict):
                results.append({
                    "method": None,
                    "status": "400",
                    "response": {"detail": "operation must be an object"},
                })
                continue

            result = {"method": operation.get("method") if isinstance(operation, dict) else None}
            if isinstance(operation, dict) and operation.get("path") is not None:
                result["path"] = operation.get("path")
            if isinstance(operation, dict) and "bulkId" in operation:
                result["bulkId"] = operation["bulkId"]

            try:
                status, response = self._bulk_execute(operation)
                result["status"] = status
                if status != "204":
                    result["response"] = response
            except VersionConflict as exc:
                result["status"] = "409"
                result["response"] = {"detail": str(exc)}
            except KeyError as exc:
                result["status"] = "404"
                result["response"] = {"detail": "resource not found: %s" % exc.args[0]}
            except ValueError as exc:
                result["status"] = "400"
                result["response"] = {"detail": str(exc)}
            except Exception as exc:
                result["status"] = "500"
                result["response"] = {"detail": str(exc)}

            results.append(result)

        return {
            "schemas": self._clone(self._BULK_SCHEMAS),
            "Operations": results,
        }
