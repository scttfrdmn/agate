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

# --- Role-session-name tenant encoding (#79) --------------------------------
# Session tags do NOT appear in Bedrock invocation logs — only the assumed-role
# ARN does, carrying the RoleSessionName. To attribute spend to a tenant UNFORGEABLY
# (rather than trusting client-supplied requestMetadata), the broker encodes the
# tenant INTO the session name as `<tenant>@<subject>`. The meter then recovers the
# tenant from the ARN. `@` is a valid STS session-name char and is NOT in the tenant
# grammar (`[a-zA-Z0-9._-]`), so it can't collide with a tenant value. STS
# RoleSessionName is <=64 chars over [\w+=,.@-]; we budget the tenant to a prefix and
# let the subject take the rest (the subject — a sub/UUID — is what must stay unique).
SESSION_TENANT_SEP = "@"
_SESSION_NAME_RE = re.compile(r"[^\w+=,.@-]")
_MAX_SESSION_NAME = 64
_MAX_SESSION_TENANT = 24  # prefix budget for the tenant; subject gets the remainder

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


# Operator role. `admin` unlocks the governed-access console (usage analytics,
# entitlement/budget management). It is NOT a tier and does not widen model/data
# access — it gates the admin surface only. Defaults to `member` (least privilege);
# only an explicit recognised admin claim promotes, so a missing/garbled claim can
# never accidentally grant admin (fail-closed, design §13.1).
Role = str
ROLE_ADMIN: Role = "admin"
ROLE_MEMBER: Role = "member"
_ADMIN_CLAIM_VALUES = frozenset({"admin", "administrator", "agate-admin"})


@dataclass(frozen=True, slots=True)
class SessionTags:
    """The `agate:` tags, validated and ready to hand to STS."""

    affiliation: Affiliation
    tenant: str
    courses: tuple[str, ...]
    tier: Tier
    role: Role = ROLE_MEMBER
    # Admin governance scope (#70 RBAC): the subtree node(s) a SCOPED admin governs,
    # e.g. ("arts-sci/chemistry",) for a chair. Empty for a non-admin, and for a
    # tenant-wide admin (whose reach is the whole tenant). This is APP-LEVEL only —
    # it is NOT emitted as an STS tag and does NOT touch IAM/tenant isolation; the
    # admin console uses it to narrow analytics to the admin's subtree. (Promoting
    # scope to an IAM principal tag for data access is a later, review-gated phase.)
    admin_scope: tuple[str, ...] = ()

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
            {"Key": tag_key("role"), "Value": self.role},
        ]


def role_session_name(tenant: str, subject: str) -> str:
    """The STS RoleSessionName the broker uses: `<tenant>@<subject>`.

    Encoding the tenant here makes it UNFORGEABLE downstream: it appears in the
    assumed-role ARN of every Bedrock/CloudTrail log line, so the spend meter can
    attribute by tenant without trusting client-supplied requestMetadata (#79).

    Sanitised to the STS session-name grammar (`[\\w+=,.@-]`, <=64). The tenant is
    budgeted to a short prefix so the subject (the uniqueness-bearing part) is never
    crowded out; both are sanitised, and a missing/empty part degrades safely.
    """
    t = _SESSION_NAME_RE.sub("-", str(tenant or "").strip())[:_MAX_SESSION_TENANT]
    # Reserve room for the separator; subject takes whatever remains of the 64.
    remaining = _MAX_SESSION_NAME - len(t) - len(SESSION_TENANT_SEP)
    s = _SESSION_NAME_RE.sub("-", str(subject or "agate-user").strip())[: max(1, remaining)]
    name = f"{t}{SESSION_TENANT_SEP}{s}" if t else s
    return name[:_MAX_SESSION_NAME] or "agate-user"


def tenant_from_session_name(session_name: str) -> str | None:
    """Recover the tenant the broker encoded into a RoleSessionName, or None.

    The meter calls this on the `<tenant>@<subject>` RoleSessionName parsed out of
    the invocation log's assumed-role ARN. Returns None when there's no encoded
    tenant (legacy/un-encoded session), so the caller can fall back rather than
    mis-attribute. `@` cannot appear in a tenant value, so a split on the FIRST `@`
    cleanly separates tenant from subject.
    """
    if not session_name or SESSION_TENANT_SEP not in session_name:
        return None
    tenant, _, _subject = session_name.partition(SESSION_TENANT_SEP)
    return tenant or None


def subject_from_session_name(session_name: str) -> str:
    """The subject (user) part of a `<tenant>@<subject>` RoleSessionName.

    Falls back to the whole name for a legacy/un-encoded session (no `@`)."""
    if SESSION_TENANT_SEP not in (session_name or ""):
        return session_name or "unknown"
    _tenant, _, subject = session_name.partition(SESSION_TENANT_SEP)
    return subject or "unknown"


def _normalise_role(raw: object) -> Role:
    """admin ONLY when the claim explicitly says so; everything else is member.

    Fail-closed: an unrecognised, missing, or malformed role claim yields `member`,
    never `admin`. Admin is an opt-in promotion, like the grant->frontier path.
    """
    if raw is None:
        return ROLE_MEMBER
    if isinstance(raw, str):
        values = re.split(r"[;,]\s*", raw)
    elif isinstance(raw, (list, tuple)):
        values = [str(v) for v in raw]
    else:
        values = [str(raw)]
    is_admin = any(v.strip().lower() in _ADMIN_CLAIM_VALUES for v in values)
    return ROLE_ADMIN if is_admin else ROLE_MEMBER


# Scope-path segment chars (a node like "arts-sci/chemistry"); `/` separates levels.
_SCOPE_RE = re.compile(r"[^a-zA-Z0-9._/-]")


def _normalise_admin_scope(raw: object, *, role: Role) -> tuple[str, ...]:
    """The subtree node(s) a SCOPED admin governs, from an `admin_scope` claim.

    Only meaningful for an admin; a non-admin always gets () regardless of the claim
    (so a forged admin_scope on a member is inert). An admin with NO admin_scope is a
    TENANT-WIDE admin (also (), interpreted by the console as the whole tenant).
    Values are sanitised to the scope-path grammar; empties dropped.
    """
    if role != ROLE_ADMIN or raw is None:
        return ()
    if isinstance(raw, str):
        items = re.split(r"[;,]\s*", raw)
    elif isinstance(raw, (list, tuple)):
        items = [str(v) for v in raw]
    else:
        items = [str(raw)]
    cleaned = []
    seen: set[str] = set()
    for item in items:
        node = _SCOPE_RE.sub("", item.strip()).strip("/")
        if node and node not in seen:
            seen.add(node)
            cleaned.append(node)
    return tuple(cleaned)


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
      * role         <- "role" | "agate_role" | "isAdmin"  (admin -> console access)

    Raises ClaimsError if the tenant cannot be determined — the broker must then
    fail closed and vend no credentials.
    """
    get = _claim_getter(claims)

    affiliation = _normalise_affiliation(get("affiliation", "edupersonaffiliation"))
    tenant = _normalise_tenant(get("tenant", "schachomeorganization", "department", "dept"))
    courses = _normalise_courses(get("courses", "enrolledcourses", "course_ids"))
    grant = _truthy(get("grant", "granttagged", "grant_tagged"))
    role = _normalise_role(get("role", "agate_role", "isadmin"))
    admin_scope = _normalise_admin_scope(get("admin_scope", "scope", "governs"), role=role)

    tier = derive_tier(affiliation, grant=grant)

    return SessionTags(
        affiliation=affiliation,
        tenant=tenant,
        courses=courses,
        tier=tier,
        role=role,
        admin_scope=admin_scope,
    )


def _claim_getter(claims: dict[str, object]):
    """Case-insensitive multi-alias lookup over the claims dict."""
    lowered = {str(k).lower(): v for k, v in claims.items()}

    def get(*aliases: str) -> object:
        for a in aliases:
            if a in lowered and lowered[a] not in (None, ""):
                return lowered[a]
        return None

    return get
