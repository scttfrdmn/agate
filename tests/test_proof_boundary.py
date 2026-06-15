"""Live drift guard for the effective-boundary view (#108): every ALLOW the view claims
is `allowed` in IAM, and every DENIAL it claims is denied. This is the heart of #108 —
no drift between the plain-language explanation and the actual enforcement.

Uses `iam:SimulateCustomPolicy` against the SAME compiled policies a deployed agent
carries, under the agent's principal tags. Read-only; skipped without AWS creds.

Run explicitly:  AWS_PROFILE=aws uv run pytest -m aws tests/test_proof_boundary.py -v
"""

from __future__ import annotations

import json

import pytest
from agate.agentcompile import compile_agent
from agate.agentspec import parse_spec
from agate.boundary import describe
from agate.entitlements import foundation_model_arn, models_for_tier
from agate.names import tag_key

REGION = "us-east-1"
ACCOUNT = "111122223333"
DOCS_BUCKET = "agate-docs-111122223333-us-east-1"

boto3 = pytest.importorskip("boto3")

_SPEC = parse_spec(
    {
        "agent": "chem101-ta",
        "description": "d",
        "role": "ta",  # oss
        "scope": "chemistry/chem-101",
        "reasoning": "lit-review",
        "tools": ["course-materials-reader"],  # read-only, no write tool
        "budget": "$20 / student / term",
    }
)
_COMPILED = compile_agent(_SPEC, region=REGION, account=ACCOUNT, bucket=DOCS_BUCKET)
_VIEW = describe(_COMPILED)


@pytest.fixture(scope="module")
def iam_client():
    client = boto3.client("iam", region_name=REGION)
    try:
        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable AWS credentials for live simulation: {exc}")
    return client


def _tags() -> list[dict]:
    t = _COMPILED.tags_template
    return [
        {"ContextKeyName": f"aws:PrincipalTag/{tag_key('tenant')}", "ContextKeyType": "string",
         "ContextKeyValues": ["chem"]},
        {"ContextKeyName": f"aws:PrincipalTag/{tag_key('tier')}", "ContextKeyType": "string",
         "ContextKeyValues": [t.tier]},
        {"ContextKeyName": f"aws:PrincipalTag/{tag_key('scope')}", "ContextKeyType": "string",
         "ContextKeyValues": [t.scope]},
    ]


def _eval(iam_client, *, policy: dict, action: str, resource: str) -> str:
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(policy)],
        ActionNames=[action],
        ResourceArns=[resource],
        ContextEntries=_tags(),
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


def _allowed(decision: str) -> bool:
    return decision == "allowed"


def _denied(decision: str) -> bool:
    return decision in ("implicitDeny", "explicitDeny")


# --- the view's claims, checked against live IAM ----------------------------


@pytest.mark.aws
def test_view_claims_oss_and_iam_allows_oss(iam_client):
    # The view CLAIMS oss models. Prove IAM allows an oss model under the agent tags.
    assert any(i.kind == "model" and i.allow for i in _VIEW.allows)
    oss = foundation_model_arn("openai.gpt-oss-20b-1:0", region=REGION)
    d = _eval(
        iam_client, policy=_COMPILED.model_access_policy, action="bedrock:Converse", resource=oss
    )
    assert _allowed(d)


@pytest.mark.aws
def test_view_denies_frontier_and_iam_denies_frontier(iam_client):
    # The view CLAIMS it cannot invoke higher-tier models. Prove IAM denies frontier.
    assert any(i.kind == "model" and not i.allow for i in _VIEW.denials)
    opus = foundation_model_arn(
        "us.anthropic.claude-opus-4-1-20250805-v1:0", region=REGION, account=ACCOUNT
    )
    d = _eval(
        iam_client, policy=_COMPILED.model_access_policy, action="bedrock:Converse", resource=opus
    )
    assert _denied(d)


@pytest.mark.aws
def test_view_claims_subtree_read_and_iam_allows_it(iam_client):
    # The view CLAIMS reads under {tenant}/chemistry/chem-101/. Prove IAM allows it.
    d = _eval(
        iam_client, policy=_COMPILED.data_scope_policy, action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/chemistry/chem-101/wk.pdf",
    )
    assert _allowed(d)


@pytest.mark.aws
def test_view_denies_outside_subtree_and_iam_denies_it(iam_client):
    # The view CLAIMS it cannot read outside its subtree. Prove IAM denies a sibling.
    d = _eval(
        iam_client, policy=_COMPILED.data_scope_policy, action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/physics/phys-101/wk.pdf",
    )
    assert _denied(d)


@pytest.mark.aws
def test_view_claims_read_tool_and_iam_allows_read_in_scope(iam_client):
    # The view CLAIMS a read-only course-materials tool. Prove the tool policy allows it.
    d = _eval(
        iam_client, policy=_COMPILED.tool_policy, action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/chemistry/chem-101/notes.pdf",
    )
    assert _allowed(d)


@pytest.mark.aws
def test_view_denies_undeclared_tool_action_and_iam_denies_it(iam_client):
    # The view CLAIMS no tool beyond those listed (this spec declared NO write tool).
    # Prove a write is denied by the tool policy.
    d = _eval(
        iam_client, policy=_COMPILED.tool_policy, action="s3:PutObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/chemistry/chem-101/notes.pdf",
    )
    assert _denied(d)


@pytest.mark.aws
def test_view_does_not_understate_models(iam_client):
    # SECURITY: the view must not UNDER-state. Every oss model the agent can actually
    # invoke must be NAMED in the view's model line (no surprise capability).
    model_line = next(i.detail for i in _VIEW.allows if i.kind == "model")
    for m in models_for_tier("oss"):
        assert m in model_line
