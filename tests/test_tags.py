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
    # exactly the four agate: tags, all namespaced
    assert len(sts) == 4
    assert all(d["Key"].startswith("agate:") for d in sts)


def test_all_tag_values_within_aws_limit():
    t = _tags(
        affiliation="researcher",
        tenant="x" * 300,
        courses=[f"C{i:05d}" for i in range(200)],
        grant=True,
    )
    for d in t.to_sts_tags():
        assert len(d["Value"]) <= MAX_TAG_VALUE_LEN
