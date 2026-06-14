"""Unit tests for the load-bearing claims -> session-tag translation.

No AWS, no boto3, no network. This is the security-critical core (security memo
§10.1), so the matrix is exhaustive on the things that decide scope: affiliation
normalisation, tier derivation, tenant fail-closed, course sanitisation, and the
256-char session-tag value bound.
"""

from __future__ import annotations

import pytest
from agate.names import tag_key
from agate.tags import (
    MAX_TAG_VALUE_LEN,
    ClaimsError,
    SessionTags,
    claims_to_tags,
)


def _tags(**claims) -> SessionTags:
    return claims_to_tags(claims)


# --- affiliation normalisation ---------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("student", "student"),
        ("faculty", "faculty"),
        ("staff", "staff"),
        ("employee", "staff"),  # alias
        ("researcher", "researcher"),
        ("research", "researcher"),  # alias
        ("STUDENT", "student"),  # case-insensitive
        (None, "student"),  # missing -> least privilege
        ("alum", "student"),  # unrecognised -> least privilege
    ],
)
def test_affiliation_normalisation(raw, expected):
    assert _tags(affiliation=raw, tenant="chem").affiliation == expected


def test_multivalued_affiliation_picks_most_privileged():
    # A work-study student who is also staff should not be under-served.
    assert _tags(affiliation=["student", "staff"], tenant="chem").affiliation == "staff"
    assert _tags(affiliation="student;researcher", tenant="chem").affiliation == "researcher"


# --- tier derivation --------------------------------------------------------


@pytest.mark.parametrize(
    "affiliation,expected_tier",
    [
        ("student", "oss"),
        ("staff", "mid"),
        ("faculty", "mid"),
        ("researcher", "frontier"),
        (None, "oss"),
    ],
)
def test_tier_derivation(affiliation, expected_tier):
    assert _tags(affiliation=affiliation, tenant="chem").tier == expected_tier


def test_grant_flag_promotes_to_frontier():
    # Grant-tagged beats base affiliation (design §5).
    assert _tags(affiliation="student", tenant="chem", grant=True).tier == "frontier"
    assert _tags(affiliation="student", tenant="chem", grant="true").tier == "frontier"
    assert _tags(affiliation="student", tenant="chem", grant=False).tier == "oss"


# --- tenant: the isolation key, must fail closed ----------------------------


def test_missing_tenant_fails_closed():
    with pytest.raises(ClaimsError):
        _tags(affiliation="faculty")


def test_empty_tenant_fails_closed():
    with pytest.raises(ClaimsError):
        _tags(affiliation="faculty", tenant="   ")


def test_tenant_aliases_and_sanitisation():
    t1 = _tags(affiliation="faculty", schacHomeOrganization="harvard.edu")
    assert t1.tenant == "harvard.edu"
    assert _tags(affiliation="faculty", department="Chem Dept!").tenant == "Chem-Dept"


def test_tenant_truncated_to_limit():
    long = "x" * 400
    assert len(_tags(affiliation="faculty", tenant=long).tenant) == MAX_TAG_VALUE_LEN


# --- courses ----------------------------------------------------------------


def test_courses_dedupe_sort_sanitise():
    # Course ids are opaque LMS identifiers and IAM StringLike is case-sensitive,
    # so case is preserved (CHEM-101 != chem-101); exact dupes are dropped, slash stripped.
    t = _tags(affiliation="student", tenant="chem", courses=["CHEM-101", "CHEM-101 ", "BIO/200"])
    assert t.courses == ("BIO200", "CHEM-101")


def test_courses_from_delimited_string():
    assert _tags(affiliation="student", tenant="chem", courses="A1, A2; A3").courses == (
        "A1",
        "A2",
        "A3",
    )


def test_courses_absent_is_empty():
    assert _tags(affiliation="faculty", tenant="chem").courses == ()


def test_courses_value_truncated_without_splitting_id():
    many = [f"COURSE{i:04d}" for i in range(100)]  # joined far exceeds 256
    t = _tags(affiliation="student", tenant="chem", courses=many)
    joined = ",".join(t.courses)
    assert len(joined) <= MAX_TAG_VALUE_LEN
    # no id was cut mid-string
    assert all(c.startswith("COURSE") and len(c) == 10 for c in t.courses)


# --- the STS tag shape (what the broker hands to AssumeRole) ----------------


def test_to_sts_tags_shape():
    t = _tags(affiliation="researcher", tenant="kempner", courses=["CS50", "CS51"], grant=True)
    sts = t.to_sts_tags()
    by_key = {d["Key"]: d["Value"] for d in sts}
    assert by_key[tag_key("affiliation")] == "researcher"
    assert by_key[tag_key("tenant")] == "kempner"
    assert by_key[tag_key("courses")] == "CS50,CS51"
    assert by_key[tag_key("tier")] == "frontier"
    assert by_key[tag_key("role")] == "member"  # default, no admin claim
    # exactly the five agate: tags, all namespaced
    assert len(sts) == 5
    assert all(d["Key"].startswith("agate:") for d in sts)


# --- role (admin) normalisation: fail-closed --------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("admin", "admin"),
        ("administrator", "admin"),
        ("agate-admin", "admin"),
        ("ADMIN", "admin"),  # case-insensitive
        (["student", "admin"], "admin"),  # multi-valued
        (None, "member"),  # missing -> member
        ("", "member"),
        ("superuser", "member"),  # unrecognised -> member, never admin
        ("user", "member"),
    ],
)
def test_role_is_fail_closed(raw, expected):
    assert _tags(tenant="t", role=raw).role == expected


def test_role_absent_defaults_to_member():
    assert _tags(tenant="t").role == "member"


# --- admin_scope (RBAC subtree, #70) — fail-closed --------------------------


def test_admin_scope_only_for_admin():
    # A member with an admin_scope claim gets NO scope (forged claim is inert).
    assert _tags(tenant="t", role="student", admin_scope="arts-sci/chemistry").admin_scope == ()


def test_admin_scope_for_admin_is_normalised():
    tags = _tags(tenant="t", role="admin", admin_scope="arts-sci/chemistry, arts-sci/physics")
    assert tags.admin_scope == ("arts-sci/chemistry", "arts-sci/physics")


def test_admin_with_no_scope_is_tenant_wide():
    # An admin with no admin_scope claim governs the whole tenant -> ().
    assert _tags(tenant="t", role="admin").admin_scope == ()


def test_admin_scope_sanitises_and_dedupes():
    tags = _tags(tenant="t", role="admin", admin_scope=["/a/b/", "a/b", "x y!"])
    assert tags.admin_scope == ("a/b", "xy")  # dedup + stripped + sanitised


# --- data-access scope -> agate:scope IAM tag (#80) -------------------------


def test_no_data_scope_means_tenant_wide_and_five_tags():
    # The common case: no data_scope claim -> scope "" -> agate:scope NOT emitted, so
    # exactly the original 5 tags (no regression for unscoped sessions).
    tags = _tags(tenant="chem")
    assert tags.scope == ""
    keys = [d["Key"] for d in tags.to_sts_tags()]
    assert tag_key("scope") not in keys
    assert len(keys) == 5


def test_single_data_scope_emitted_as_agate_scope_tag():
    tags = _tags(tenant="chem", data_scope="arts-sci/chemistry")
    assert tags.scope == "arts-sci/chemistry"
    by_key = {d["Key"]: d["Value"] for d in tags.to_sts_tags()}
    assert by_key[tag_key("scope")] == "arts-sci/chemistry"  # path '/' preserved
    assert len(by_key) == 6  # 5 + scope


def test_multi_data_scope_fails_closed_to_tenant_wide():
    # An IAM principal tag is a single scalar; a multi-subtree claim can't be
    # confined to both-but-not-the-rest -> "" (tenant-wide), never cross-tenant.
    assert _tags(tenant="chem", data_scope=["chemistry", "physics"]).scope == ""
    assert _tags(tenant="chem", data_scope="chemistry, physics").scope == ""


def test_garbled_or_missing_data_scope_is_empty():
    assert _tags(tenant="chem").scope == ""
    assert _tags(tenant="chem", data_scope="").scope == ""
    assert _tags(tenant="chem", data_scope="  ///  ").scope == ""  # sanitises to nothing


def test_data_scope_independent_of_admin_scope():
    # A member with a data_scope gets the data tag but NO admin_scope (forged inert).
    tags = _tags(tenant="chem", data_scope="chemistry", admin_scope="physics")
    assert tags.scope == "chemistry"
    assert tags.admin_scope == ()  # member -> admin_scope inert


def test_data_scope_within_aws_tag_limit():
    tags = _tags(tenant="chem", data_scope="a/" * 200)
    assert len(tags.scope) <= MAX_TAG_VALUE_LEN


# --- role-session-name tenant encoding (#79) --------------------------------

from agate.tags import (  # noqa: E402
    role_session_name,
    subject_from_session_name,
    tenant_from_session_name,
)


def test_role_session_name_encodes_tenant():
    assert role_session_name("kempner", "u123") == "kempner@u123"


def test_tenant_round_trips_through_session_name():
    name = role_session_name("arts-sci", "64684478-b031-70ad-4ba0-8b3386c99b46")
    assert tenant_from_session_name(name) == "arts-sci"
    assert subject_from_session_name(name) == "64684478-b031-70ad-4ba0-8b3386c99b46"


def test_legacy_session_name_without_at_has_no_tenant():
    # An un-encoded (legacy) session name -> tenant None, subject is the whole name.
    assert tenant_from_session_name("student-7") is None
    assert subject_from_session_name("student-7") == "student-7"


def test_session_name_within_sts_limit_and_charset():
    name = role_session_name("a" * 50, "b" * 80)
    assert len(name) <= 64
    assert all(c.isalnum() or c in "+=,.@-_" for c in name)


def test_session_name_sanitises_unsafe_chars():
    # '@' in a subject can't smuggle a second separator that fakes a tenant: the
    # tenant is taken from the FIRST '@', and the broker passes the verified tenant.
    name = role_session_name("chem", "evil@x")
    assert tenant_from_session_name(name) == "chem"


def test_all_tag_values_within_aws_limit():
    t = _tags(
        affiliation="researcher",
        tenant="x" * 300,
        courses=[f"C{i:05d}" for i in range(200)],
        grant=True,
    )
    for d in t.to_sts_tags():
        assert len(d["Value"]) <= MAX_TAG_VALUE_LEN
