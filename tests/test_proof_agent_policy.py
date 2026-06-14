"""Live proof: a compiled agent's policies grant EXACTLY its spec's tier + scope + tools
and nothing broader (#105, vision §1). Runs the SAME generated policies a deployed agent
would carry through IAM's simulator with the agent's `agate:` principal tags.

Like the other proofs (`test_proof_tenant_isolation.py`) this uses
`iam:SimulateCustomPolicy` — read-only, deterministic, no deployed resources. Skipped
without AWS creds.

Run explicitly:  AWS_PROFILE=aws uv run pytest -m aws tests/test_proof_agent_policy.py -v
"""

from __future__ import annotations

import json

import pytest
from agate.agentcompile import compile_agent
from agate.agentspec import parse_spec
from agate.entitlements import foundation_model_arn
from agate.names import tag_key

REGION = "us-east-1"
ACCOUNT = "111122223333"
DOCS_BUCKET = "agate-docs-111122223333-us-east-1"

boto3 = pytest.importorskip("boto3")


# A representative agent: a TA (oss tier) confined to chemistry/chem-101, with a
# read-only course-materials tool. The compiled policies are what a deployed agent
# carries; the principal tags are what bounded delegation (#106) would stamp at spawn.
_SPEC = parse_spec(
    {
        "agent": "chem101-ta",
        "description": "Drafts feedback for instructor review.",
        "role": "ta",
        "scope": "chemistry/chem-101",
        "reasoning": "lit-review",
        "tools": ["course-materials-reader"],
    }
)


@pytest.fixture(scope="module")
def compiled():
    return compile_agent(_SPEC, region=REGION, account=ACCOUNT, bucket=DOCS_BUCKET)


@pytest.fixture(scope="module")
def iam_client():
    client = boto3.client("iam", region_name=REGION)
    try:
        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable AWS credentials for live simulation: {exc}")
    return client


def _agent_tags(scope: str | None = "chemistry/chem-101", tier: str = "oss") -> list[dict]:
    """Principal tags a spawned agent would carry (tenant + tier + optional scope)."""
    entries = [
        {
            "ContextKeyName": f"aws:PrincipalTag/{tag_key('tenant')}",
            "ContextKeyType": "string",
            "ContextKeyValues": ["chem"],
        },
        {
            "ContextKeyName": f"aws:PrincipalTag/{tag_key('tier')}",
            "ContextKeyType": "string",
            "ContextKeyValues": [tier],
        },
    ]
    if scope is not None:
        entries.append(
            {
                "ContextKeyName": f"aws:PrincipalTag/{tag_key('scope')}",
                "ContextKeyType": "string",
                "ContextKeyValues": [scope],
            }
        )
    return entries


def _eval(iam_client, *, policy: dict, action: str, resource: str, tags: list[dict]) -> str:
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(policy)],
        ActionNames=[action],
        ResourceArns=[resource],
        ContextEntries=tags,
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


# --- model axis: confined to the spec's tier --------------------------------


@pytest.mark.aws
def test_entitled_tier_model_is_allowed(iam_client, compiled):
    # An oss model the TA is entitled to → Converse allowed.
    oss_arn = foundation_model_arn("openai.gpt-oss-20b-1:0", region=REGION)
    d = _eval(
        iam_client,
        policy=compiled.model_access_policy,
        action="bedrock:Converse",
        resource=oss_arn,
        tags=_agent_tags(tier="oss"),
    )
    assert d == "allowed"


@pytest.mark.aws
def test_higher_tier_model_is_denied(iam_client, compiled):
    # A frontier model the oss-tier agent is NOT entitled to → denied.
    opus = foundation_model_arn(
        "us.anthropic.claude-opus-4-1-20250805-v1:0", region=REGION, account=ACCOUNT
    )
    d = _eval(
        iam_client,
        policy=compiled.model_access_policy,
        action="bedrock:Converse",
        resource=opus,
        tags=_agent_tags(tier="oss"),
    )
    assert d in ("implicitDeny", "explicitDeny")


# --- data axis: confined to the spec's scope subtree (#80) ------------------


@pytest.mark.aws
def test_doc_within_scope_subtree_allowed(iam_client, compiled):
    d = _eval(
        iam_client,
        policy=compiled.data_scope_policy,
        action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/chemistry/chem-101/wk.pdf",
        tags=_agent_tags(),
    )
    assert d == "allowed"


@pytest.mark.aws
def test_doc_in_sibling_subtree_denied(iam_client, compiled):
    d = _eval(
        iam_client,
        policy=compiled.data_scope_policy,
        action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/physics/phys-101/wk.pdf",
        tags=_agent_tags(),
    )
    assert d == "explicitDeny"


@pytest.mark.aws
def test_doc_cross_tenant_denied(iam_client, compiled):
    d = _eval(
        iam_client,
        policy=compiled.data_scope_policy,
        action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/psych/chemistry/chem-101/wk.pdf",
        tags=_agent_tags(),
    )
    assert d in ("implicitDeny", "explicitDeny")


# --- tool axis (the new boundary): exactly the declared tool, nothing more ---


@pytest.mark.aws
def test_declared_tool_allows_read_in_scope(iam_client, compiled):
    # course-materials-reader → GetObject within the tenant/scope subtree.
    d = _eval(
        iam_client,
        policy=compiled.tool_policy,
        action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/chemistry/chem-101/notes.pdf",
        tags=_agent_tags(),
    )
    assert d == "allowed"


@pytest.mark.aws
def test_undeclared_write_action_is_denied(iam_client, compiled):
    # The spec declared ONLY a read tool; a write must not be granted (denied by absence).
    d = _eval(
        iam_client,
        policy=compiled.tool_policy,
        action="s3:PutObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/chemistry/chem-101/notes.pdf",
        tags=_agent_tags(),
    )
    assert d in ("implicitDeny", "explicitDeny")


@pytest.mark.aws
def test_tool_read_outside_scope_is_not_allowed(iam_client, compiled):
    # The tool grant is scope-fenced: a read in a sibling subtree isn't allowed by it.
    d = _eval(
        iam_client,
        policy=compiled.tool_policy,
        action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/physics/phys-101/notes.pdf",
        tags=_agent_tags(),
    )
    assert d in ("implicitDeny", "explicitDeny")
