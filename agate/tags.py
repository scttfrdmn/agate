"""claims_to_tags — the part we own (design §3.1).

Pure, side-effect-free translation of campus-IdP claims into the `agate:` ABAC
session-tag set. This is the single most security-critical piece of logic in the
system (security memo §10.1: "credential-vending mis-scope ... the single most
important thing to review"), so it is deliberately:

  * pure — no AWS, no I/O, no clock; fully unit-testable
  * least-privilege by default — unknown/missing claims narrow, never widen
  * AWS-constraint-aware — values are normalised to the STS session-tag rules so
    the downstream AssumeRole cannot fail on a malformed tag (which would either
    deny the user or, worse, drop a scoping tag).

The broker Lambda calls this and passes the result verbatim as STS `Tags`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agate.entitlements import Affiliation, Tier, derive_tier
from agate.names import tag_key

# --- AWS session-tag constraints (STS AssumeRole) ---------------------------
# Keys <=128 chars, values <=256 chars, up to 50 tags. Allowed chars:
# letters, digits, and + - = . _ : / @  (and whitespace). We are stricter on
# values we synthesise (course ids, tenant) to avoid surprises.
MAX_TAG_VALUE_LEN = 256
_TENANT_RE = re.compile(r"[^a-zA-Z0-9._-]")
_COURSE_RE = re.compile(r"[^a-zA-Z0-9._-]")
COURSE_SEP = ","

# eduPersonAffiliation synonyms we fold into our normalised set.
_AFFILIATION_ALIASES: dict[str, Affiliation] = {
    "student": "student",
    "faculty": "faculty",
    "staff": "staff",
    "employee": "staff",
    "researcher": "researcher",
    "research": "researcher",
}


class ClaimsError(ValueError):
    """Raised when claims are too malformed to scope safely.

    The broker MUST treat this as a hard deny (vend no credentials) rather than
    falling back to a broad scope — failing closed is the whole point.
    """


@dataclass(frozen=True, slots=True)
class SessionTags:
    """The four `agate:` tags, validated and ready to hand to STS."""

    affiliation: Affiliation
    tenant: str
    courses: tuple[str, ...]
    tier: Tier

    def to_sts_tags(self) -> list[dict[str, str]]:
        """STS AssumeRole `Tags` form: [{"Key": "agate:affiliation", "Value": ...}, ...].

        `agate:courses` is a comma-joined list (session-tag values are scalar
        strings); IAM policies match it with StringLike `*course*` conditions.
        """
        return [
            {"Key": tag_key("affiliation"), "Value": self.affiliation},
            {"Key": tag_key("tenant"), "Value": self.tenant},
            {"Key": tag_key("courses"), "Value": COURSE_SEP.join(self.courses)},
            {"Key": tag_key("tier"), "Value": self.tier},
        ]


def _normalise_affiliation(raw: object) -> Affiliation:
    """Pick the most-privileged recognised affiliation from a claim.

    eduPersonAffiliation is multi-valued; a person can be both `student` and
    `staff`. We choose the affiliation that derives the highest tier so a
    work-study student employee isn't under-served — entitlement is still
    bounded by the resulting tier's model set.
    """
    values: list[str]
    if raw is None:
        values = []
    elif isinstance(raw, str):
        # Accept comma- or semicolon-separated multi-values in a single string.
        values = re.split(r"[;,]\s*", raw)
    elif isinstance(raw, (list, tuple)):
        values = [str(v) for v in raw]
    else:
        values = [str(raw)]

    recognised = {
        _AFFILIATION_ALIASES[v.strip().lower()]
        for v in values
        if v.strip().lower() in _AFFILIATION_ALIASES
    }
    if not recognised:
        # No recognised affiliation -> least privilege, but still a valid member.
        return "student"
    # Most-privileged wins (ranked by the tier it derives).
    return max(recognised, key=lambda a: _tier_rank(derive_tier(a)))


def _tier_rank(tier: Tier) -> int:
    from agate.entitlements import TIER_RANK

    return TIER_RANK[tier]


def _normalise_tenant(raw: object) -> str:
    """The tenant is the isolation key — it must be present and clean."""
    if raw is None:
        raise ClaimsError("missing tenant claim; cannot scope data access")
    tenant = _TENANT_RE.sub("-", str(raw).strip()).strip("-")
    if not tenant:
        raise ClaimsError("tenant claim empty after normalisation")
    return tenant[:MAX_TAG_VALUE_LEN]


def _normalise_courses(raw: object) -> tuple[str, ...]:
    """Dedupe, sort, sanitise, and truncate the enrolled-course list.

    Course ids drive retrieval scope. They come from LTI NRPS later; for now we
    accept a list or a delimited string. We sort for determinism (stable tag
    value -> stable policy-cache behaviour) and truncate the joined value to the
    256-char session-tag limit, dropping overflow rather than corrupting the tag.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        items = re.split(r"[;,]\s*", raw)
    elif isinstance(raw, (list, tuple)):
        items = [str(v) for v in raw]
    else:
        items = [str(raw)]

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        c = _COURSE_RE.sub("", item.strip())
        if c and c not in seen:
            seen.add(c)
            cleaned.append(c)
    cleaned.sort()

    # Truncate to the joined-value length limit without splitting an id.
    out: list[str] = []
    length = 0
    for c in cleaned:
        added = len(c) + (1 if out else 0)
        if length + added > MAX_TAG_VALUE_LEN:
            break
        out.append(c)
        length += added
    return tuple(out)


def _truthy(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "t")


def claims_to_tags(claims: dict[str, object]) -> SessionTags:
    """Translate IdP claims into the validated `agate:` session-tag set.

    Recognised claim keys (case-insensitive on common aliases):
      * affiliation  <- "affiliation" | "eduPersonAffiliation" | "eduperson_affiliation"
      * tenant       <- "tenant" | "schacHomeOrganization" | "department" | "dept"
      * courses      <- "courses" | "enrolledCourses" | "course_ids"  (list or delimited str)
      * grant        <- "grant" | "grantTagged"  (truthy -> promote to frontier)

    Raises ClaimsError if the tenant cannot be determined — the broker must then
    fail closed and vend no credentials.
    """
    get = _claim_getter(claims)

    affiliation = _normalise_affiliation(get("affiliation", "edupersonaffiliation"))
    tenant = _normalise_tenant(get("tenant", "schachomeorganization", "department", "dept"))
    courses = _normalise_courses(get("courses", "enrolledcourses", "course_ids"))
    grant = _truthy(get("grant", "granttagged", "grant_tagged"))

    tier = derive_tier(affiliation, grant=grant)

    return SessionTags(affiliation=affiliation, tenant=tenant, courses=courses, tier=tier)


def _claim_getter(claims: dict[str, object]):
    """Case-insensitive multi-alias lookup over the claims dict."""
    lowered = {str(k).lower(): v for k, v in claims.items()}

    def get(*aliases: str) -> object:
        for a in aliases:
            if a in lowered and lowered[a] not in (None, ""):
                return lowered[a]
        return None

    return get
