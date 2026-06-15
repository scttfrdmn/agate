"""Build IAM policy documents from the single-source-of-truth entitlement table.

Two policies make up the authenticated role's effective scope (design §13.2/§13.3):

  * model-access  — `bedrock:Converse*` / `InvokeModel*` allowed only for the
    model ARNs entitled to the session's `agate:tier`.
  * data-scope    — `s3:GetObject` and the S3 Vectors query action allowed only
    within the session's `agate:tenant` (the principal tag is the isolation key).

Everything is deny-by-default: the role grants nothing except via these Allow
statements, and an explicit Deny guards the data path against a missing tenant tag.
"""

from __future__ import annotations

from agate.entitlements import TIERS, model_arns_for_tier
from agate.names import DOCS_BUCKET_PREFIX, HANDLE, tag_key

BEDROCK_INVOKE_ACTIONS = [
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
]


def model_access_policy(region: str = "*", account: str = "") -> dict:
    """One Allow per tier, gated on `agate:tier` == that tier.

    Tiers are cumulative (model_arns_for_tier includes lower tiers), so a
    `frontier` session matches the frontier statement and gets the full set,
    while an `oss` session matches only the oss statement. No statement ->
    implicit deny. This is the §13.2 "tier->model map as a managed policy"
    expressed without inline branches: the ARNs come straight from the table.
    """
    statements = []
    for tier in TIERS:
        arns = model_arns_for_tier(tier, region=region, account=account)
        statements.append(
            {
                "Sid": f"InvokeTier{tier.capitalize()}",
                "Effect": "Allow",
                "Action": BEDROCK_INVOKE_ACTIONS,
                "Resource": arns,
                "Condition": {"StringEquals": {f"aws:PrincipalTag/{tag_key('tier')}": tier}},
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def data_scope_policy(bucket: str | None = None) -> dict:
    """S3 + S3 Vectors reads scoped to `${aws:PrincipalTag/agate:tenant}`.

    The principal tag is interpolated into the resource ARN, so the credential
    itself cannot read another tenant's prefix (security memo §6). A guard Deny
    rejects the whole data path if the tenant tag is absent — fail closed rather
    than fall through to a broad match.

    Additionally (#80), when the session carries an `agate:scope` principal tag, S3
    document reads are CONFINED to `{tenant}/{scope}/` (strict containment). The
    confinement Denies are `Null:false`-guarded, so an unscoped session keeps
    tenant-wide access (no regression).

    Vectors (#84): this browser-held role carries NO `s3vectors` query grant. Vector
    retrieval goes through the server-side proxy (`agate-vector-reader` +
    `vector_query_policy`), which injects the scope filter from the verified token —
    so scope is a real boundary, not an advisory client-supplied filter.
    """
    docs_bucket = bucket or f"{DOCS_BUCKET_PREFIX}-*"
    tenant_tag = f"${{aws:PrincipalTag/{tag_key('tenant')}}}"
    # Optional per-session DATA-scope confinement (#80): when the session carries an
    # `agate:scope` principal tag, S3 document reads are confined to `{tenant}/{scope}/`
    # (strict containment — tenant-root + sibling subtrees are denied). The tag is a
    # single scope path (agate.tags._normalise_data_scope); it is ABSENT for the common
    # tenant-wide case, and the Denies below are `Null:false`-guarded so an unscoped
    # session is unaffected (no regression). Vector scope is enforced by the retrieval
    # proxy, not here — see vector_query_policy (#84).
    scope_tag = f"${{aws:PrincipalTag/{tag_key('scope')}}}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                # GetObject is gated by the RESOURCE ARN (tenant interpolated into the
                # key path). NOT by s3:prefix — that context key is only populated for
                # ListBucket, so conditioning GetObject on it would never match.
                "Sid": "GetOwnTenantDocs",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{docs_bucket}/{tenant_tag}/*"],
            },
            {
                # ListBucket is a bucket-level action gated by the s3:prefix the caller
                # asks to list — confined to the tenant's prefix.
                "Sid": "ListOwnTenantDocs",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{docs_bucket}"],
                "Condition": {"StringLike": {"s3:prefix": [f"{tenant_tag}/*", tenant_tag]}},
            },
            # NOTE: the browser-held role has NO s3vectors query grant (#84). Vector
            # retrieval no longer happens browser-direct — a direct QueryVectors with
            # these creds is denied (no Allow). Vector queries go through the
            # server-side retrieval proxy, which assumes `agate-vector-reader`
            # (vector_query_policy below) so it can inject the scope filter from the
            # VERIFIED token. The browser could otherwise omit/alter that filter.
            {
                # Fail closed: if the session has no tenant tag, deny the data
                # path outright rather than risk a broad match. Keeps s3vectors:* in
                # the action list as defense-in-depth even though no vector Allow
                # remains on this role.
                "Sid": "DenyWhenNoTenantTag",
                "Effect": "Deny",
                "Action": ["s3:GetObject", "s3:ListBucket", "s3vectors:*"],
                "Resource": "*",
                "Condition": {"Null": {f"aws:PrincipalTag/{tag_key('tenant')}": "true"}},
            },
            {
                # Scope confinement for object reads (#80). Fires ONLY when the session
                # has an `agate:scope` tag (Null:false); then any GetObject whose ARN is
                # NOT under `{tenant}/{scope}/` is denied. The explicit Deny overrides the
                # tenant-wide Allow, confining a scoped session to its subtree.
                "Sid": "DenyS3GetOutsideScopeSubtree",
                "Effect": "Deny",
                "Action": ["s3:GetObject"],
                "NotResource": [f"arn:aws:s3:::{docs_bucket}/{tenant_tag}/{scope_tag}/*"],
                "Condition": {"Null": {f"aws:PrincipalTag/{tag_key('scope')}": "false"}},
            },
            {
                # Parallel confinement for listing: a scoped session may only list within
                # its subtree prefix. Same Null:false guard -> inert when unscoped.
                "Sid": "DenyS3ListOutsideScopeSubtree",
                "Effect": "Deny",
                "Action": ["s3:ListBucket"],
                "Resource": "*",
                "Condition": {
                    "Null": {f"aws:PrincipalTag/{tag_key('scope')}": "false"},
                    "StringNotLike": {"s3:prefix": [f"{tenant_tag}/{scope_tag}/*"]},
                },
            },
        ],
    }


def vector_query_policy() -> dict:
    """S3 Vectors query grant for the `agate-vector-reader` role (#84).

    This is the ONLY role that may query vectors. It is assumed solely by the
    server-side retrieval proxy (its trust policy names only the proxy's exec role —
    the browser cannot assume it), so every vector query goes through code that
    injects the scope filter from the verified token. The tenant fence stays in IAM
    (`aws:ResourceTag/agate:tenant == ${aws:PrincipalTag/agate:tenant}`), preserving
    the CISO promise that cross-tenant reads are denied by the credential; SCOPE is
    enforced by the proxy (it cannot be IAM-enforced — per-tenant index, row-metadata
    scope). This is the moved-out `QueryOwnTenantVectors` statement plus its guard.
    """
    tenant_tag = f"${{aws:PrincipalTag/{tag_key('tenant')}}}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "QueryOwnTenantVectors",
                "Effect": "Allow",
                # S3 Vectors query actions (GA). Verify the exact action names for the
                # target region before deploy; fenced by tenant tag here.
                "Action": ["s3vectors:QueryVectors", "s3vectors:GetVectors"],
                "Resource": "*",
                "Condition": {"StringEquals": {f"aws:ResourceTag/{tag_key('tenant')}": tenant_tag}},
            },
            {
                # Fail closed: no tenant tag -> deny all vector access (the proxy always
                # assumes this role WITH a tenant tag from the verified token).
                "Sid": "DenyVectorsWhenNoTenantTag",
                "Effect": "Deny",
                "Action": ["s3vectors:*"],
                "Resource": "*",
                "Condition": {"Null": {f"aws:PrincipalTag/{tag_key('tenant')}": "true"}},
            },
        ],
    }


# How each capability resource_kind maps to a tenant/scope-fenced ARN. Kept here (with
# the other emitters) so policy JSON has ONE source. The compiler (agate.agentcompile)
# resolves a spec's declared tools to (sid, actions, resource_kind, write) tuples and
# passes them in — policy.generate stays free of any agate.agentspec import.
def _tool_resource(resource_kind: str, bucket: str, gateway_arn: str) -> str:
    tenant_tag = f"${{aws:PrincipalTag/{tag_key('tenant')}}}"
    scope_tag = f"${{aws:PrincipalTag/{tag_key('scope')}}}"
    if resource_kind == "docs-scope":
        # Read within the session's tenant+scope subtree (mirrors data_scope_policy #80).
        return f"arn:aws:s3:::{bucket}/{tenant_tag}/{scope_tag}/*"
    if resource_kind == "drafts-queue":
        # Writes land in a DRAFT prefix, never a live system (vision §5).
        return f"arn:aws:s3:::{bucket}/{tenant_tag}/{scope_tag}/_drafts/*"
    if resource_kind == "vector-read":
        return "*"  # vectors fenced by tenant ResourceTag, not ARN (see vector_query_policy)
    if resource_kind == "gateway-tool":
        # A campus MCP tool reached via AgentCore Gateway (#113). IAM fences WHICH tools
        # the agent may invoke (the Gateway ARN family); the call still carries the
        # agent's agate:scope tag, and the tool's effect is bounded by that scope + the
        # budget cascade (a write/submit) + user-delegated OAuth — IAM=which, scope/OAuth
        # =effect (§5). An undeclared tool produces no Allow → denied by absence.
        return gateway_arn
    raise ValueError(f"unknown tool resource_kind: {resource_kind!r}")


# Default gateway-tool ARN family — tenant-fenced by INTERPOLATION (not `*`), so even the
# pure template keeps the IAM tenant boundary the way the S3 kinds do (a too-broad default
# would let an agent invoke another tenant's gateway, since gateway ARNs aren't scope-
# interpolated like S3 keys). The deploy passes a concrete region/account ARN; this
# default at least confines invocation to THIS tenant's gateway family by principal tag.
_DEFAULT_GATEWAY_ARN = (
    f"arn:aws:bedrock-agentcore:*:*:gateway/{HANDLE}-"
    f"${{aws:PrincipalTag/{tag_key('tenant')}}}-*"
)


def agent_tool_policy(
    grants: list[dict], bucket: str | None = None, gateway_arn: str | None = None
) -> dict:
    """The tool grants for a compiled agent (#105). One Allow per declared capability;
    EVERY resource is interpolated with the agent's `agate:tenant` + `agate:scope`
    principal tags (S3 tools) or fenced to the gateway ARN family (campus MCP tools,
    #113), so a tool can never reach beyond what the agent declared. An undeclared tool
    produces no statement → implicit deny (tools are denied by absence).

    `grants` is a list of `{sid, actions, resource_kind, write}` dicts the compiler
    builds from the spec's tools (resolved against `agentspec` capabilities). `gateway_arn`
    is the AgentCore Gateway ARN family campus tools resolve to; the default is
    `_DEFAULT_GATEWAY_ARN` — **tenant-fenced by principal-tag interpolation, never `*`** —
    so even the pure template keeps the IAM tenant boundary (the deploy supplies a concrete
    region/account ARN). Returns a deny-only fail-closed doc when there are no tools."""
    docs_bucket = bucket or f"{DOCS_BUCKET_PREFIX}-*"
    gw_arn = gateway_arn or _DEFAULT_GATEWAY_ARN
    statements = [
        {
            "Sid": g["sid"],
            "Effect": "Allow",
            "Action": list(g["actions"]),
            "Resource": [_tool_resource(g["resource_kind"], docs_bucket, gw_arn)],
        }
        for g in grants
    ]
    # Fail closed: with no tenant tag, deny every tool action regardless of grants.
    tool_actions = sorted({a for g in grants for a in g["actions"]}) or ["s3:GetObject"]
    statements.append(
        {
            "Sid": "DenyToolsWhenNoTenantTag",
            "Effect": "Deny",
            "Action": tool_actions,
            "Resource": "*",
            "Condition": {"Null": {f"aws:PrincipalTag/{tag_key('tenant')}": "true"}},
        }
    )
    return {"Version": "2012-10-17", "Statement": statements}


# AgentCore Memory actions a session uses: read its records + append conversation events.
# (Verify exact action names against the target region before deploy.)
MEMORY_READ_ACTIONS = ["bedrock-agentcore:RetrieveMemoryRecords"]
MEMORY_WRITE_ACTIONS = ["bedrock-agentcore:CreateEvent"]


def memory_access_policy(memory_arn: str = "*") -> dict:
    """Cross-session memory scoped to the session's `agate:` tags (#110, vision §3).

    Memory records live under a namespace; AgentCore enforces it via the
    `bedrock-agentcore:namespacePath` condition key. We interpolate the principal tags
    into that path, so the credential itself confines memory to the session's TENANT and
    (for shared memory) its SCOPE — the same `${aws:PrincipalTag/...}` discipline as
    `data_scope_policy`. A session can read/write only under `agate/{tenant}/...`.

    Boundary split (like #84): tenant + scope are IAM-enforced here; the per-principal
    `personal/{subject}` segment is NOT (subject isn't an STS principal tag) — the
    server path supplies the exact `agate.memory.personal_namespace(tags, subject)` from
    the verified RoleSessionName, with an injective `subject_key`. So IAM fences the
    tenant; code fences the principal within it (never client-supplied).
    """
    tenant_tag = f"${{aws:PrincipalTag/{tag_key('tenant')}}}"
    scope_tag = f"${{aws:PrincipalTag/{tag_key('scope')}}}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                # All of THIS tenant's memory (personal + session live under it; the
                # subject segment is server-supplied). namespacePath is hierarchical, so
                # this StringLike confines to the tenant subtree.
                "Sid": "AccessOwnTenantMemory",
                "Effect": "Allow",
                "Action": MEMORY_READ_ACTIONS + MEMORY_WRITE_ACTIONS,
                "Resource": memory_arn,
                "Condition": {
                    "StringLike": {"bedrock-agentcore:namespacePath": f"agate/{tenant_tag}/*"}
                },
            },
            {
                # Fail closed: no tenant tag -> no memory access at all.
                "Sid": "DenyMemoryWhenNoTenantTag",
                "Effect": "Deny",
                "Action": ["bedrock-agentcore:*"],
                "Resource": "*",
                "Condition": {"Null": {f"aws:PrincipalTag/{tag_key('tenant')}": "true"}},
            },
            {
                # Shared-tier confinement: when the session carries an `agate:scope` tag,
                # deny any shared-memory access OUTSIDE `agate/{tenant}/shared/{scope}/`.
                # Null:false-guarded so an unscoped session is unaffected (it has no
                # shared tier — agate.memory returns None). Mirrors the #80 subtree Deny.
                "Sid": "DenySharedMemoryOutsideScope",
                "Effect": "Deny",
                "Action": MEMORY_READ_ACTIONS + MEMORY_WRITE_ACTIONS,
                "Resource": "*",
                "Condition": {
                    "Null": {f"aws:PrincipalTag/{tag_key('scope')}": "false"},
                    "StringLike": {
                        "bedrock-agentcore:namespacePath": f"agate/{tenant_tag}/shared/*"
                    },
                    "StringNotLike": {
                        "bedrock-agentcore:namespacePath": (
                            f"agate/{tenant_tag}/shared/{scope_tag}/*"
                        )
                    },
                },
            },
        ],
    }
