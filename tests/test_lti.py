"""Unit tests for the pure LTI 1.3 claim mapping. No AWS, no JWT, no network."""

from __future__ import annotations

import pytest
from agg.lti import (
    CLAIM_CONTEXT,
    CLAIM_ROLES,
    LtiClaimError,
    affiliation_from_roles,
    course_from_context,
    lti_claims_to_agg_claims,
    nonce_is_fresh,
    state_matches,
)
from agg.tags import claims_to_tags

INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
TA = "http://purl.imsglobal.org/vocab/lis/v2/membership#Mentor"


# --- role -> affiliation ----------------------------------------------------


@pytest.mark.parametrize(
    "roles,expected",
    [
        ([INSTRUCTOR], "faculty"),
        ([LEARNER], "student"),
        (["Instructor"], "faculty"),  # short form
        (["Learner"], "student"),
        ([TA], "staff"),
        ([], "student"),  # empty -> least privilege
        (["http://.../membership#Administrator"], "staff"),
    ],
)
def test_affiliation_from_roles(roles, expected):
    assert affiliation_from_roles(roles) == expected


def test_most_privileged_role_wins():
    # A faculty member also enrolled as a learner in a course -> faculty.
    assert affiliation_from_roles([LEARNER, INSTRUCTOR]) == "faculty"


# --- context -> course ------------------------------------------------------


def test_course_from_context():
    claims = {CLAIM_CONTEXT: {"id": "CHEM-101", "label": "CHEM101", "title": "Intro Chem"}}
    assert course_from_context(claims) == "CHEM-101"


def test_course_from_context_sanitised():
    claims = {CLAIM_CONTEXT: {"id": "CHEM/101 §A"}}
    assert course_from_context(claims) == "CHEM-101--A"


def test_course_absent():
    assert course_from_context({}) is None
    assert course_from_context({CLAIM_CONTEXT: {}}) is None


# --- full mapping, end-to-end into claims_to_tags ---------------------------


def test_lti_claims_to_agg_claims_with_registration_tenant():
    claims = {
        "sub": "user-42",
        CLAIM_ROLES: [LEARNER],
        CLAIM_CONTEXT: {"id": "CHEM-101", "label": "chem"},
    }
    agg = lti_claims_to_agg_claims(claims, tenant="harvard-chem")
    assert agg["affiliation"] == "student"
    assert agg["tenant"] == "harvard-chem"
    assert agg["courses"] == ["CHEM-101"]
    assert agg["sub"] == "user-42"

    # And it flows cleanly into the Phase 1 translation.
    tags = claims_to_tags(agg)
    assert tags.affiliation == "student"
    assert tags.tier == "oss"
    assert tags.tenant == "harvard-chem"
    assert "CHEM-101" in tags.courses


def test_instructor_launch_maps_to_faculty_mid_tier():
    claims = {
        "sub": "prof-1",
        CLAIM_ROLES: [INSTRUCTOR],
        CLAIM_CONTEXT: {"id": "CHEM-101", "label": "chem"},
    }
    tags = claims_to_tags(lti_claims_to_agg_claims(claims, tenant="chem"))
    assert tags.affiliation == "faculty"
    assert tags.tier == "mid"


def test_tenant_falls_back_to_context_label():
    claims = {CLAIM_ROLES: [LEARNER], CLAIM_CONTEXT: {"id": "C1", "label": "psych-dept"}}
    agg = lti_claims_to_agg_claims(claims)
    assert agg["tenant"] == "psych-dept"


def test_no_tenant_fails_closed():
    claims = {CLAIM_ROLES: [LEARNER]}  # no context, no registration tenant
    with pytest.raises(LtiClaimError):
        lti_claims_to_agg_claims(claims)


# --- nonce / state ----------------------------------------------------------


def test_state_matches():
    assert state_matches("abc123", "abc123") is True
    assert state_matches("abc123", "abc124") is False
    assert state_matches(None, "x") is False
    assert state_matches("x", None) is False


def test_nonce_freshness():
    assert nonce_is_fresh("n1", seen=False) is True
    assert nonce_is_fresh("n1", seen=True) is False  # replay
    assert nonce_is_fresh(None, seen=False) is False
    assert nonce_is_fresh("", seen=False) is False
