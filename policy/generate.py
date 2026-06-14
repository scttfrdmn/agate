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
from agate.names import DOCS_BUCKET_PREFIX, tag_key

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
