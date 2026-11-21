import pytest

import scim_deprovision_sync as scim


def sync(**kwargs):
    return scim.ScimDeprovisionSync(**kwargs)


def user_payload(name, external=None, email=None):
    payload = {"userName": name}
    if external is not None:
        payload["externalId"] = external
    if email is not None:
        payload["emails"] = [{"value": email, "primary": True}]
    return payload


def test_create_user_returns_scim_resource():
    s = sync()
    user = s.create_user(user_payload("alice", "ext-1", "alice@example.com"))
    assert user["id"]
    assert user["userName"] == "alice"
    assert user["active"] is True
    assert user["version"] == "1"
    assert s.get_user(user["id"])["externalId"] == "ext-1"


def test_user_name_uniqueness_and_filter_are_case_insensitive():
    s = sync()
    alice = s.create_user(user_payload("Alice", "ext-1", "Alice@Example.com"))
    with pytest.raises(ValueError):
        s.create_user(user_payload("alice", "ext-2"))
    found = s.find_user('USERNAME eq "ALICE"')
    assert found["id"] == alice["id"]
    by_email = s.find_user('Emails.Value eq "alice@example.com"')
    assert by_email["id"] == alice["id"]


def test_external_id_is_globally_unique():
    s = sync()
    s.create_user(user_payload("alice", "ext-1"))
    with pytest.raises(ValueError):
        s.create_user(user_payload("bob", "ext-1"))


def test_pagination_is_one_based_and_covers_every_user():
    s = sync(page_size=2)
    created = [s.create_user(user_payload("user%d" % i, "ext-%d" % i))["id"]
               for i in range(5)]
    seen = []
    start = 1
    while True:
        page = s.list_users(start_index=start, count=2)
        assert page["startIndex"] == start
        assert page["totalResults"] == 5
        seen.extend(item["id"] for item in page["Resources"])
        start += page["itemsPerPage"]
        if start > page["totalResults"]:
            break
    assert seen == created
    assert len(set(seen)) == 5


def test_invalid_start_index_is_rejected():
    s = sync()
    with pytest.raises(ValueError):
        s.list_users(start_index=0)
    with pytest.raises(ValueError):
        s.list_users(start_index=-1)


def test_patch_group_removes_member_by_filter_path():
    s = sync()
    u1 = s.create_user(user_payload("u1", "ext-1"))["id"]
    u2 = s.create_user(user_payload("u2", "ext-2"))["id"]
    group = s.create_group({"displayName": "eng"})
    s.add_member(group["id"], u1)
    s.add_member(group["id"], u2)
    updated = s.patch_group(group["id"], [
        {"op": "remove", "path": 'members[value eq "%s"]' % u2}
    ])
    assert updated["members"] == [u1]
    assert s.effective_groups(u2) == []


def test_unsupported_patch_paths_raise_instead_of_being_skipped():
    s = sync()
    user = s.create_user(user_payload("alice", "ext-1"))
    with pytest.raises(ValueError):
        s.patch_user(user["id"], [
            {"op": "replace", "path": "displayName", "value": "Alice"}
        ])
    group = s.create_group({"displayName": "eng"})
    with pytest.raises(ValueError):
        s.patch_group(group["id"], [
            {"op": "remove", "path": 'members[value ne "u1"]'}
        ])


def test_nested_groups_propagate_to_parent():
    s = sync()
    user = s.create_user(user_payload("alice", "ext-1"))["id"]
    parent = s.create_group({"displayName": "all"})
    child = s.create_group({"displayName": "eng", "parent": parent["id"]})
    s.add_member(child["id"], user)
    assert s.effective_groups(user) == sorted([parent["id"], child["id"]])


def test_patch_active_false_propagates_and_revokes_credentials():
    s = sync()
    user = s.create_user(user_payload("alice", "ext-1"))["id"]
    group = s.create_group({"displayName": "eng"})
    s.add_member(group["id"], user)
    s.create_session(user)
    updated = s.patch_user(user, [
        {"op": "replace", "path": "active", "value": False}
    ])
    assert updated["active"] is False
    assert s.effective_groups(user) == []
    assert s.sessions(user) == []
    assert s.tokens(user) == []
    with pytest.raises(ValueError):
        s.create_session(user)


def test_deactivate_is_idempotent():
    s = sync()
    user = s.create_user(user_payload("alice", "ext-1"))["id"]
    first = s.deactivate_user(user)
    second = s.deactivate_user(user)
    assert first["active"] is False
    assert second["active"] is False
    assert second["version"] == first["version"]


def test_delete_removes_user_and_credentials():
    s = sync()
    user = s.create_user(user_payload("alice", "ext-1"))["id"]
    group = s.create_group({"displayName": "eng"})
    s.add_member(group["id"], user)
    s.create_session(user)
    result = s.delete_user(user)
    assert result["deleted"] is True
    with pytest.raises(KeyError):
        s.get_user(user)
    assert s.effective_groups(user) == []
    assert s.sessions(user) == []
    assert s.tokens(user) == []


def test_delete_is_idempotent():
    s = sync()
    user = s.create_user(user_payload("alice", "ext-1"))["id"]
    assert s.delete_user(user)["deleted"] is True
    assert s.delete_user(user)["deleted"] is True


def test_deleted_account_is_not_resurrected_by_resync():
    s = sync()
    user = s.create_user(user_payload("alice", "ext-1"))["id"]
    s.delete_user(user)
    with pytest.raises(ValueError):
        s.create_user(user_payload("alice", "ext-1"))
    assert s.find_user('externalId eq "ext-1"') is None


def test_stale_version_is_rejected_without_overwriting():
    s = sync()
    user = s.create_user(user_payload("alice", "ext-1", "old@example.com"))
    updated = s.update_user(user["id"], {"emails": [{"value": "new@example.com"}]},
                            version=user["version"])
    assert updated["version"] == "2"
    with pytest.raises(scim.VersionConflict):
        s.update_user(user["id"], {"userName": "changed"}, version="1")
    with pytest.raises(scim.VersionConflict):
        s.patch_user(user["id"], [{"op": "replace", "path": "active", "value": False}],
                     version="1")
    current = s.get_user(user["id"])
    assert current["userName"] == "alice"
    assert current["active"] is True


def test_bulk_reports_each_operation_independently():
    s = sync()
    result = s.bulk([
        {"method": "POST", "path": "/Users",
         "data": user_payload("carol", "ext-c")},
        {"method": "DELETE", "path": "/Users/missing"},
        {"method": "POST", "path": "/Users",
         "data": user_payload("dave", "ext-c")},
    ])
    statuses = [operation["status"] for operation in result["Operations"]]
    assert statuses[0].startswith("2")
    assert statuses[1] == "404"
    assert statuses[2] == "400"
    assert s.find_user('userName eq "carol"') is not None
    assert s.find_user('userName eq "dave"') is None
