"""Phase 1 end-to-end proof (design §12): tag-scoped Converse allow/deny.

This is the acceptance test for the load-bearing crux. It takes the SAME generated
model-access policy the CDK stack attaches, hands it to IAM's policy simulator with
an `agate:tier` principal-tag context, and asserts:

  * an entitled model ARN -> `Converse` ALLOWED
  * a non-entitled (higher-tier) model ARN -> `Converse` DENIED

Using iam:SimulateCustomPolicy proves the IAM evaluation itself (not our reading of
it) enforces the tag scope, with no standing IdP and no live model invocation —
read-only and deterministic. It is skipped automatically when AWS creds are absent
so the pure unit suite still runs offline.

Run explicitly:  uv run pytest -m aws tests/test_proof_simulation.py -v
"""

from __future__ import annotations

import json

import pytest
from agate.entitlements import TIER_MODELS, foundation_model_arn
from agate.names import tag_key
from policy.generate import model_access_policy

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


def _simulate(iam_client, *, tier: str, resource_arn: str) -> str:
    """Return the IAM eval decision for bedrock:Converse on resource_arn at `tier`."""
    policy_json = json.dumps(model_access_policy(region=REGION))
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[policy_json],
        ActionNames=["bedrock:Converse"],
        ResourceArns=[resource_arn],
        ContextEntries=[
            {
                "ContextKeyName": f"aws:PrincipalTag/{tag_key('tier')}",
                # Principal tags are scalar strings; StringEquals won't match a
                # stringList context. This must be "string".
                "ContextKeyType": "string",
                "ContextKeyValues": [tier],
            }
        ],
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


# Pick one representative ARN per tier from the single-source-of-truth table.
OSS_MODEL = foundation_model_arn(TIER_MODELS["oss"][0], region=REGION)
FRONTIER_MODEL = foundation_model_arn(TIER_MODELS["frontier"][0], region=REGION)


@pytest.mark.aws
def test_oss_session_allowed_its_own_model(iam_client):
    assert _simulate(iam_client, tier="oss", resource_arn=OSS_MODEL) == "allowed"


@pytest.mark.aws
def test_oss_session_denied_frontier_model(iam_client):
    # The whole point: a student-tier session cannot reach a frontier model.
    assert _simulate(iam_client, tier="oss", resource_arn=FRONTIER_MODEL) == "implicitDeny"


@pytest.mark.aws
def test_frontier_session_allowed_frontier_model(iam_client):
    assert _simulate(iam_client, tier="frontier", resource_arn=FRONTIER_MODEL) == "allowed"


@pytest.mark.aws
def test_frontier_session_allowed_lower_tier_model(iam_client):
    # Cumulative entitlement: frontier may also invoke oss/mid models.
    assert _simulate(iam_client, tier="frontier", resource_arn=OSS_MODEL) == "allowed"
