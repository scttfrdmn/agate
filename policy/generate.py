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
    """
    docs_bucket = bucket or f"{DOCS_BUCKET_PREFIX}-*"
    tenant_tag = f"${{aws:PrincipalTag/{tag_key('tenant')}}}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadOwnTenantDocs",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{docs_bucket}",
                    f"arn:aws:s3:::{docs_bucket}/{tenant_tag}/*",
                ],
                "Condition": {"StringLike": {"s3:prefix": [f"{tenant_tag}/*", tenant_tag]}},
            },
            {
                "Sid": "QueryOwnTenantVectors",
                "Effect": "Allow",
                # S3 Vectors query action (GA). Verify the exact action name for
                # the target region before deploy; scope it by tenant tag here.
                "Action": ["s3vectors:QueryVectors", "s3vectors:GetVectors"],
                "Resource": "*",
                "Condition": {"StringEquals": {f"aws:ResourceTag/{tag_key('tenant')}": tenant_tag}},
            },
            {
                # Fail closed: if the session has no tenant tag, deny the data
                # path outright rather than risk a broad match.
                "Sid": "DenyWhenNoTenantTag",
                "Effect": "Deny",
                "Action": ["s3:GetObject", "s3:ListBucket", "s3vectors:*"],
                "Resource": "*",
                "Condition": {"Null": {f"aws:PrincipalTag/{tag_key('tenant')}": "true"}},
            },
        ],
    }
