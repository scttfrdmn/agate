"""Phase 3 end-to-end proof: tenant-scoped S3 Vectors retrieval (design §12).

The FERPA-critical claim (security memo §6): a CHEM-101-scoped session can query
ONLY its own tenant's index. This takes the SAME generated data-scope policy the
Phase 1 identity stack attaches and runs it through IAM's simulator with an
`agg:tenant` principal tag and a target index carrying its own `agg:tenant`
resource tag, asserting:

  * same-tenant index  -> QueryVectors ALLOWED
  * cross-tenant index -> QueryVectors DENIED

Like the Phase 1 model-scope proof this uses iam:SimulateCustomPolicy — read-only,
deterministic, no deployed resources. Skipped without AWS creds.

Run explicitly:  AWS_PROFILE=aws uv run pytest -m aws tests/test_proof_tenant_isolation.py -v
"""

from __future__ import annotations

import json

import pytest
from agg.names import tag_key
from policy.generate import data_scope_policy

REGION = "us-east-1"

boto3 = pytest.importorskip("boto3")


@pytest.fixture(scope="module")
def iam_client():
    client = boto3.client("iam", region_name=REGION)
    try:
        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable AWS credentials for live simulation: {exc}")
    return client


def _simulate_query(iam_client, *, session_tenant: str, index_tenant: str) -> str:
    """Eval s3vectors:QueryVectors for a session in `session_tenant` against an
    index tagged with `index_tenant`."""
    policy_json = json.dumps(data_scope_policy())
    # A representative S3 Vectors index ARN (the resource the policy guards).
    index_arn = (
        f"arn:aws:s3vectors:{REGION}:111122223333:bucket/agg-vectors/index/agg-{index_tenant}"
    )
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[policy_json],
        ActionNames=["s3vectors:QueryVectors"],
        ResourceArns=[index_arn],
        ContextEntries=[
            {
                "ContextKeyName": f"aws:PrincipalTag/{tag_key('tenant')}",
                "ContextKeyType": "string",
                "ContextKeyValues": [session_tenant],
            },
            {
                "ContextKeyName": f"aws:ResourceTag/{tag_key('tenant')}",
                "ContextKeyType": "string",
                "ContextKeyValues": [index_tenant],
            },
        ],
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


@pytest.mark.aws
def test_same_tenant_query_allowed(iam_client):
    assert _simulate_query(iam_client, session_tenant="chem", index_tenant="chem") == "allowed"


@pytest.mark.aws
def test_cross_tenant_query_denied(iam_client):
    # The FERPA nightmare case: a chem session must NOT read the psych index.
    decision = _simulate_query(iam_client, session_tenant="chem", index_tenant="psych")
    assert decision in ("implicitDeny", "explicitDeny")


@pytest.mark.aws
def test_other_direction_also_denied(iam_client):
    decision = _simulate_query(iam_client, session_tenant="psych", index_tenant="chem")
    assert decision in ("implicitDeny", "explicitDeny")
