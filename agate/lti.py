"""Pure LTI 1.3 claim mapping — the bridge from an LMS launch to agate's tags.

Side-effect-free and AWS-free (design §6, §13.5). An LTI 1.3 launch carries the
roster context agate needs: who the user is (roles) and which course they launched
from (context + NRPS). This module turns a *validated* LTI id_token's claims into
the plain claims dict that Phase 1's `claims_to_tags()` consumes — so LTI becomes
one concrete source of `agate:affiliation` / `agate:courses`, with no second tag scheme.

Signature verification, nonce checks, and JWKS fetching are NOT here — they are the
I/O edges in lti/handler.py. This module assumes the token is already authentic and
only does the (pure, testable) claim translation and the nonce/state bookkeeping.

LTI 1.3 claim URIs and the role vocabulary are from the IMS Global LTI 1.3 /
LIS role specs.
"""

from __future__ import annotations

import re

# --- LTI 1.3 message claim URIs ---------------------------------------------
CLAIM_MESSAGE_TYPE = "https://purl.imsglobal.org/spec/lti/claim/message_type"
CLAIM_ROLES = "https://purl.imsglobal.org/spec/lti/claim/roles"
CLAIM_CONTEXT = "https://purl.imsglobal.org/spec/lti/claim/context"
CLAIM_DEPLOYMENT_ID = "https://purl.imsglobal.org/spec/lti/claim/deployment_id"
CLAIM_NRPS = "https://purl.imsglobal.org/spec/lti-nrps/claim/namesroleservice"
CLAIM_CUSTOM = "https://purl.imsglobal.org/spec/lti/claim/custom"

MSG_RESOURCE_LINK = "LtiResourceLinkRequest"
MSG_DEEP_LINKING = "LtiDeepLinkingRequest"

# --- LIS / LTI role vocabulary ----------------------------------------------
# Roles arrive as a list of URIs (or short names). We classify by substring on the
# role's terminal segment so both the full URI form
# (http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor) and the short
# form (Instructor) map the same way.
_ROLE_INSTRUCTOR = ("instructor", "faculty", "teacher")
_ROLE_STAFF = ("staff", "administrator", "mentor", "contentdeveloper", "ta", "teachingassistant")
_ROLE_LEARNER = ("learner", "student")
# An institution role of Faculty/Staff present alongside a course Learner role is
# common (a faculty member auditing a course); affiliation is decided by the most
# privileged role seen, consistent with claims_to_tags' multi-affiliation handling.


class LtiClaimError(ValueError):
    """The launch is missing claims required to scope a session — fail closed."""


def _role_segment(role: str) -> str:
    """Terminal segment of a role URI, lowercased: the '#Instructor' tail or the
    bare short name."""
    tail = re.split(r"[#/]", role.strip())[-1]
    return tail.lower()


def affiliation_from_roles(roles: list[str]) -> str:
    """Map LTI roles -> an eduPerson-style affiliation for claims_to_tags.

    Returns the most-privileged recognised affiliation. Unknown/empty roles fall
    back to `student` (least privilege), matching the broker's default posture.
    """
    segs = {_role_segment(r) for r in roles or []}

    def has(group: tuple[str, ...]) -> bool:
        return any(any(g in s for g in group) for s in segs)

    # Instructor/faculty outrank staff outrank learner.
    if has(_ROLE_INSTRUCTOR):
        return "faculty"
    if has(_ROLE_STAFF):
        return "staff"
    if has(_ROLE_LEARNER):
        return "student"
    return "student"


def course_from_context(claims: dict) -> str | None:
    """The course id from the LTI context claim (the launched course)."""
    ctx = claims.get(CLAIM_CONTEXT) or {}
    cid = ctx.get("id") if isinstance(ctx, dict) else None
    if not cid:
        return None
    # Sanitise to the session-tag charset (claims_to_tags re-sanitises too, but a
    # clean value keeps the audit trail readable).
    return re.sub(r"[^a-zA-Z0-9._-]", "-", str(cid)).strip("-") or None


def lti_claims_to_agate_claims(claims: dict, *, tenant: str | None = None) -> dict:
    """Translate a validated LTI id_token claim set into the dict that Phase 1's
    `claims_to_tags()` consumes.

    - affiliation  <- LTI roles
    - courses      <- LTI context id (the launched course); NRPS can add more later
    - tenant       <- the REGISTRATION tenant ONLY. The tenant is the data-isolation
                      key (-> agate:tenant tag -> S3 prefix + vector index), so it is an
                      institutional decision tied to the registered platform and is
                      NEVER derived from the id_token. The LTI context claim (course
                      label/id) is set by course creators, i.e. attacker-influenceable
                      on a shared LMS; falling back to it would let a user pick another
                      tenant's corpus (SEC-3). We fail closed instead.

    Raises LtiClaimError if the registration carries no tenant, so the broker vends
    no session rather than an attacker-scoped one.
    """
    roles = claims.get(CLAIM_ROLES) or []
    if not isinstance(roles, list):
        roles = [roles]

    affiliation = affiliation_from_roles(roles)

    course = course_from_context(claims)
    courses = [course] if course else []

    # Tenant comes only from the registration (a server-side trusted value). No
    # fallback to any token-carried claim — see SEC-3.
    resolved_tenant = tenant
    if not resolved_tenant:
        raise LtiClaimError(
            "registration carries no tenant; refusing to derive it from the launch token"
        )

    return {
        "affiliation": affiliation,
        "tenant": resolved_tenant,
        "courses": courses,
        # `sub` carries through for the broker's RoleSessionName / audit trail.
        "sub": claims.get("sub"),
    }


# --- Nonce / state bookkeeping (pure decisions) -----------------------------
# The handlers persist these in DynamoDB; the *decisions* live here so they are
# testable without AWS. An OIDC launch must echo back the exact state we issued and
# present a nonce we issued and have not yet seen (replay protection).


def state_matches(issued_state: str | None, returned_state: str | None) -> bool:
    """The launch's `state` must equal the value we set at login init."""
    if not issued_state or not returned_state:
        return False
    return _consteq(issued_state, returned_state)


def nonce_is_fresh(nonce: str | None, seen: bool) -> bool:
    """A nonce is acceptable iff it is present and we have not already consumed it."""
    return bool(nonce) and not seen


def _consteq(a: str, b: str) -> bool:
    """Constant-time-ish string compare (avoid early-exit timing on state/nonce)."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b, strict=True):
        result |= ord(x) ^ ord(y)
    return result == 0
