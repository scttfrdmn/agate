"""Live proof: a spawned agent's credential can never out-scope its spawner (#106, §2).

Bounded delegation (`agate.delegate.delegate`) intersects the spawner's verified tags
with the agent spec to produce the CHILD tags. This runs the SAME generated policies a
deployed agent carries through IAM's simulator UNDER THE CHILD TAGS, proving the child
is confined to the intersection — a chemistry-scoped spawner can never produce a child
that reads physics, and a child's tier never exceeds the min of spawner and spec.

Like the other proofs this uses `iam:SimulateCustomPolicy` — read-only, deterministic,
no deployed resources. Skipped without AWS creds.

Run explicitly:  AWS_PROFILE=aws uv run pytest -m aws tests/test_proof_delegation.py -v
"""

from __future__ import annotations

import json

import pytest
from agate.agentspec import parse_spec
from agate.delegate import delegate
from agate.entitlements import foundation_model_arn
from agate.names import tag_key
from agate.tags import SessionTags
from policy.generate import data_scope_policy, model_access_policy

REGION = "us-east-1"
ACCOUNT = "111122223333"
DOCS_BUCKET = "agate-docs-111122223333-us-east-1"

boto3 = pytest.importorskip("boto3")


def _spec(role="ta", scope="chemistry/chem-101"):
    return parse_spec(
        {"agent": "a", "description": "d", "role": role, "scope": scope, "reasoning": "lit-review"}
    )


# A frontier-tier faculty spawner confined to the `chemistry` subtree. The CHILD is the
# intersection with a TA spec at chemistry/chem-101 → oss tier, chemistry/chem-101 scope.
_SPAWNER = SessionTags(
    affiliation="faculty", tenant="chem", courses=("chem-101",), tier="frontier", scope="chemistry"
)
_CHILD = delegate(_SPAWNER, _spec())


@pytest.fixture(scope="module")
def iam_client():
    client = boto3.client("iam", region_name=REGION)
    try:
        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable AWS credentials for live simulation: {exc}")
    return client


def _child_tags() -> list[dict]:
    """Principal-tag context entries for the spawned child's verified tags."""
    return [
        {
            "ContextKeyName": f"aws:PrincipalTag/{tag_key('tenant')}",
            "ContextKeyType": "string",
            "ContextKeyValues": [_CHILD.tenant],
        },
        {
            "ContextKeyName": f"aws:PrincipalTag/{tag_key('tier')}",
            "ContextKeyType": "string",
            "ContextKeyValues": [_CHILD.tier],
        },
        {
            "ContextKeyName": f"aws:PrincipalTag/{tag_key('scope')}",
            "ContextKeyType": "string",
            "ContextKeyValues": [_CHILD.scope],
        },
    ]


def _eval(iam_client, *, policy: dict, action: str, resource: str) -> str:
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(policy)],
        ActionNames=[action],
        ResourceArns=[resource],
        ContextEntries=_child_tags(),
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


def test_child_tier_is_min_of_spawner_and_spec():
    # frontier spawner ∩ ta spec → oss (pure check, anchors the live proof below).
    assert _CHILD.tier == "oss"
    assert _CHILD.scope == "chemistry/chem-101"


# --- the child is confined to the intersection, proven in IAM ---------------


@pytest.mark.aws
def test_child_reads_within_its_narrowed_subtree(iam_client):
    d = _eval(
        iam_client,
        policy=data_scope_policy(bucket=DOCS_BUCKET),
        action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/chemistry/chem-101/wk.pdf",
    )
    assert d == "allowed"


@pytest.mark.aws
def test_child_cannot_read_parent_scope_outside_its_subtree(iam_client):
    # The spawner could read all of `chemistry`; the child (chem-101) cannot read a
    # sibling course under chemistry — it was narrowed BELOW the spawner.
    d = _eval(
        iam_client,
        policy=data_scope_policy(bucket=DOCS_BUCKET),
        action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/chemistry/chem-202/wk.pdf",
    )
    assert d == "explicitDeny"


@pytest.mark.aws
def test_child_cannot_read_physics_the_headline_guarantee(iam_client):
    # A chemistry-scoped spawner can NEVER produce a child that reads physics.
    d = _eval(
        iam_client,
        policy=data_scope_policy(bucket=DOCS_BUCKET),
        action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/physics/phys-101/wk.pdf",
    )
    assert d == "explicitDeny"


@pytest.mark.aws
def test_child_cannot_invoke_above_its_min_tier(iam_client):
    # The child is oss (min of frontier spawner + ta spec); a frontier model is denied
    # even though the SPAWNER was frontier — the child was narrowed below it.
    opus = foundation_model_arn(
        "us.anthropic.claude-opus-4-1-20250805-v1:0", region=REGION, account=ACCOUNT
    )
    d = _eval(
        iam_client,
        policy=model_access_policy(region=REGION, account=ACCOUNT),
        action="bedrock:Converse",
        resource=opus,
    )
    assert d in ("implicitDeny", "explicitDeny")


@pytest.mark.aws
def test_child_can_invoke_within_its_tier(iam_client):
    oss_arn = foundation_model_arn("openai.gpt-oss-20b-1:0", region=REGION)
    d = _eval(
        iam_client,
        policy=model_access_policy(region=REGION, account=ACCOUNT),
        action="bedrock:Converse",
        resource=oss_arn,
    )
    assert d == "allowed"


@pytest.mark.aws
def test_child_cross_tenant_still_denied(iam_client):
    d = _eval(
        iam_client,
        policy=data_scope_policy(bucket=DOCS_BUCKET),
        action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/psych/chemistry/chem-101/wk.pdf",
    )
    assert d in ("implicitDeny", "explicitDeny")
