import pytest

import ldap_nested_group_fold as lnf


class FakeDirectory:
    def __init__(self):
        self.groups = {}
        self.versions = {}
        self.known = set()

    def set_group(self, dn, members=None, pages=None, referrals=None,
                  truncated=False, missing_members=False, version="v1"):
        self.groups[dn] = {
            "members": list(members or []),
            "pages": pages,
            "referrals": list(referrals or []),
            "truncated": truncated,
            "missing_members": missing_members,
        }
        self.versions[dn] = version
        self.known.add(dn)

    def update(self, dn, members):
        self.groups[dn]["members"] = list(members)
        self.versions[dn] = self.versions[dn] + "+"

    def rename(self, old, new):
        self.groups[new] = self.groups.pop(old)
        self.versions[new] = self.versions.pop(old) + "+"
        self.known.add(new)

    def delete(self, dn):
        del self.groups[dn]

    def fetch(self, dn, cursor=None):
        if dn not in self.groups:
            raise KeyError(dn)
        record = self.groups[dn]
        if record["pages"] is not None:
            index = int(cursor or 0)
            pages = record["pages"]
            return {
                "dn": dn,
                "members": list(pages[index]),
                "referrals": [],
                "next_cursor": str(index + 1) if index + 1 < len(pages) else None,
                "version": self.versions[dn],
                "truncated": False,
            }
        result = {
            "dn": dn,
            "referrals": list(record["referrals"]),
            "next_cursor": None,
            "version": self.versions[dn],
            "truncated": record["truncated"],
        }
        if not record["missing_members"]:
            result["members"] = list(record["members"])
        return result

    def is_group(self, dn):
        return dn in self.known

    def list_groups(self):
        return sorted(self.groups)


USER1 = "uid=u1,ou=people,dc=corp"
USER2 = "uid=u2,ou=people,dc=corp"
USER3 = "uid=u3,ou=people,dc=corp"


def fold(directory, **kwargs):
    return lnf.LdapNestedGroupFold(directory, **kwargs)


def test_canonical_dn_normalizes_case_and_spacing():
    f = fold(FakeDirectory())
    assert f.canonical_dn("CN = Ops ,  OU = Eng , DC = Corp ") == "cn=ops,ou=eng,dc=corp"
    assert f.canonical_dn("cn=ops,ou=eng,dc=corp") == "cn=ops,ou=eng,dc=corp"


def test_escaped_comma_is_not_an_rdn_separator():
    f = fold(FakeDirectory())
    escaped = f.canonical_dn("CN=Ops\\, West,DC=corp")
    assert escaped == "cn=ops\\, west,dc=corp"
    assert escaped != f.canonical_dn("CN=Ops,DC=corp")


def test_direct_members_are_returned_canonically():
    d = FakeDirectory()
    d.set_group("cn=team,dc=corp", members=[USER1, "CN=Sub,DC=corp"])
    f = fold(d)
    assert f.members("cn=team,dc=corp") == ["cn=sub,dc=corp", USER1]


def test_three_levels_of_nesting_are_expanded():
    d = FakeDirectory()
    d.set_group("cn=grandparent,dc=corp", members=["cn=parent,dc=corp"])
    d.set_group("cn=parent,dc=corp", members=["cn=child,dc=corp"])
    d.set_group("cn=child,dc=corp", members=[USER1, USER2])
    f = fold(d)
    assert f.effective_members("cn=grandparent,dc=corp") == {USER1, USER2}


def test_group_cycle_raises():
    d = FakeDirectory()
    d.set_group("cn=a,dc=corp", members=["cn=b,dc=corp"])
    d.set_group("cn=b,dc=corp", members=["cn=a,dc=corp"])
    with pytest.raises(ValueError):
        fold(d).effective_members("cn=a,dc=corp")


def test_depth_limit_is_enforced():
    d = FakeDirectory()
    d.set_group("cn=a,dc=corp", members=["cn=b,dc=corp"])
    d.set_group("cn=b,dc=corp", members=["cn=c,dc=corp"])
    d.set_group("cn=c,dc=corp", members=[USER1])
    assert fold(d, max_depth=2).effective_members("cn=a,dc=corp") == {USER1}
    d.set_group("cn=c,dc=corp", members=["cn=d,dc=corp"])
    d.set_group("cn=d,dc=corp", members=[USER1])
    with pytest.raises(ValueError):
        fold(d, max_depth=2).effective_members("cn=a,dc=corp")


def test_paginated_members_are_all_fetched():
    d = FakeDirectory()
    d.set_group("cn=big,dc=corp", pages=[
        [USER1, USER2], [USER3, "uid=u4,ou=people,dc=corp"],
        ["uid=u5,ou=people,dc=corp"],
    ])
    f = fold(d, page_limit=10)
    assert f.effective_members("cn=big,dc=corp") == {
        USER1, USER2, USER3,
        "uid=u4,ou=people,dc=corp",
        "uid=u5,ou=people,dc=corp",
    }


def test_empty_member_attribute_differs_from_missing_attribute():
    d = FakeDirectory()
    d.set_group("cn=empty,dc=corp", members=[])
    assert fold(d).effective_members("cn=empty,dc=corp") == set()
    d.set_group("cn=broken,dc=corp", missing_members=True)
    with pytest.raises(ValueError):
        fold(d).effective_members("cn=broken,dc=corp")


def test_truncated_result_is_an_explicit_error():
    d = FakeDirectory()
    d.set_group("cn=cut,dc=corp", members=[USER1], truncated=True)
    with pytest.raises(lnf.SizeLimitExceeded):
        fold(d).effective_members("cn=cut,dc=corp")


def test_referrals_follow_only_allowed_hosts():
    d = FakeDirectory()
    d.set_group("cn=team,dc=corp", members=[USER1],
                referrals=["ldap://other.example/cn=ext,dc=other"])
    d.set_group("ldap://other.example/cn=ext,dc=other",
                members=["uid=ext,dc=other"])
    default = fold(d, referral_allowlist=["other.example"])
    assert default.effective_members("cn=team,dc=corp") == {USER1}
    allowed = fold(d, follow_referrals=True, referral_allowlist=["other.example"])
    assert allowed.effective_members("cn=team,dc=corp") == {USER1, "uid=ext,dc=other"}
    denied = fold(d, follow_referrals=True, referral_allowlist=["trusted.example"])
    assert denied.effective_members("cn=team,dc=corp") == {USER1}


def test_version_change_invalidates_cached_group():
    d = FakeDirectory()
    d.set_group("cn=team,dc=corp", members=[USER1])
    f = fold(d)
    assert f.effective_members("cn=team,dc=corp") == {USER1}
    d.update("cn=team,dc=corp", [USER1, USER2])
    assert f.effective_members("cn=team,dc=corp") == {USER1, USER2}


def test_renamed_group_is_not_served_from_stale_cache():
    d = FakeDirectory()
    d.set_group("cn=old,dc=corp", members=[USER1])
    f = fold(d)
    assert f.effective_members("cn=old,dc=corp") == {USER1}
    d.rename("cn=old,dc=corp", "cn=new,dc=corp")
    with pytest.raises(KeyError):
        f.effective_members("cn=old,dc=corp")
    assert f.effective_members("cn=new,dc=corp") == {USER1}


def test_deleted_nested_group_loses_authorization():
    d = FakeDirectory()
    d.set_group("cn=parent,dc=corp", members=["cn=child,dc=corp"])
    d.set_group("cn=child,dc=corp", members=[USER1])
    f = fold(d)
    assert f.effective_members("cn=parent,dc=corp") == {USER1}
    d.delete("cn=child,dc=corp")
    assert f.effective_members("cn=parent,dc=corp") == set()
    assert f.groups_for(USER1) == set()


def test_same_named_groups_in_different_ous_stay_separate():
    d = FakeDirectory()
    d.set_group("cn=dev,ou=a,dc=corp", members=[USER1])
    d.set_group("cn=dev,ou=b,dc=corp", members=[USER2])
    f = fold(d)
    assert f.effective_members("cn=dev,ou=a,dc=corp") == {USER1}
    assert f.effective_members("cn=dev,ou=b,dc=corp") == {USER2}
    assert f.groups_for(USER1) == {"cn=dev,ou=a,dc=corp"}


def test_groups_for_walks_ancestors():
    d = FakeDirectory()
    d.set_group("cn=all,dc=corp", members=["cn=eng,dc=corp"])
    d.set_group("cn=eng,dc=corp", members=["cn=team,dc=corp"])
    d.set_group("cn=team,dc=corp", members=[USER1])
    f = fold(d)
    assert f.groups_for(USER1) == {
        "cn=all,dc=corp", "cn=eng,dc=corp", "cn=team,dc=corp",
    }
