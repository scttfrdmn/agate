"""Live proof: in an agent graph, a grandchild's credential ⊆ child ⊆ root (#111, §4).

Builds a 3-level graph with `build_graph`, then runs the generated data-scope/model
policies through IAM's simulator UNDER each node's narrowed tags — proving a grandchild
can reach only its (intersected) scope and (min) tier, and is denied things the root
could reach but the grandchild was narrowed below. Monotonic narrowing, transitive,
proven in STS.

Read-only (`iam:SimulateCustomPolicy`); skipped without AWS creds.
Run:  AWS_PROFILE=aws uv run pytest -m aws tests/test_proof_graph.py -v
"""

from __future__ import annotations

import json

import pytest
from agate.agentspec import parse_spec
from agate.entitlements import foundation_model_arn
from agate.graph import build_graph, flatten
from agate.names import tag_key
from agate.tags import SessionTags
from policy.generate import data_scope_policy, model_access_policy

REGION = "us-east-1"
ACCOUNT = "111122223333"
DOCS_BUCKET = "agate-docs-111122223333-us-east-1"

boto3 = pytest.importorskip("boto3")

# Root: a frontier researcher scoped to `chemistry`. Child `lit`: researcher at
# chemistry/chem-101 (narrows scope). Grandchild `ta`: student (narrows tier to oss),
# same scope. So the grandchild is oss + chemistry/chem-101.
_ROOT_SPEC = parse_spec(
    {
        "agent": "root",
        "description": "d",
        "role": "researcher",
        "scope": "chemistry",
        "reasoning": "lit-review",
        "agents": [
            {
                "agent": "lit",
                "description": "d",
                "role": "researcher",
                "scope": "chemistry/chem-101",
                "reasoning": "lit-review",
                "agents": [
                    {
                        "agent": "ta",
                        "description": "d",
                        "role": "student",
                        "scope": "chemistry/chem-101",
                        "reasoning": "lit-review",
                    }
                ],
            }
        ],
    }
)
_ROOT_TAGS = SessionTags(
    affiliation="researcher", tenant="chem", courses=(), tier="frontier", scope="chemistry"
)
_GRAPH = build_graph(_ROOT_SPEC, _ROOT_TAGS, subject="pi")
_NODES = {"/".join(n.path): n for n in flatten(_GRAPH)}
_GC = _NODES["root/lit/ta"]  # the grandchild


@pytest.fixture(scope="module")
def iam_client():
    client = boto3.client("iam", region_name=REGION)
    try:
        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable AWS credentials for live simulation: {exc}")
    return client


def _tags_ctx(node) -> list[dict]:
    return [
        {
            "ContextKeyName": f"aws:PrincipalTag/{tag_key('tenant')}",
            "ContextKeyType": "string",
            "ContextKeyValues": [node.tags.tenant],
        },
        {
            "ContextKeyName": f"aws:PrincipalTag/{tag_key('tier')}",
            "ContextKeyType": "string",
            "ContextKeyValues": [node.tags.tier],
        },
        {
            "ContextKeyName": f"aws:PrincipalTag/{tag_key('scope')}",
            "ContextKeyType": "string",
            "ContextKeyValues": [node.tags.scope],
        },
    ]


def _eval(iam_client, node, *, policy, action, resource) -> str:
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(policy)],
        ActionNames=[action],
        ResourceArns=[resource],
        ContextEntries=_tags_ctx(node),
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


def test_grandchild_is_narrowed_pure():
    # anchors the live proof: ta is oss + chemistry/chem-101 (min tier, intersected scope)
    assert _GC.tags.tier == "oss"
    assert _GC.tags.scope == "chemistry/chem-101"


@pytest.mark.aws
def test_grandchild_reads_only_its_narrowed_subtree(iam_client):
    dp = data_scope_policy(bucket=DOCS_BUCKET)
    inside = _eval(
        iam_client,
        _GC,
        policy=dp,
        action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/chemistry/chem-101/wk.pdf",
    )
    # The ROOT could read all of chemistry; the grandchild was narrowed to chem-101 and
    # CANNOT read a sibling course under chemistry.
    sibling = _eval(
        iam_client,
        _GC,
        policy=dp,
        action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/chem/chemistry/chem-202/wk.pdf",
    )
    assert inside == "allowed"
    assert sibling == "explicitDeny"


@pytest.mark.aws
def test_grandchild_cannot_invoke_above_its_min_tier(iam_client):
    mp = model_access_policy(region=REGION, account=ACCOUNT)
    # The root was frontier; the grandchild is oss (student) and must be denied a frontier
    # model even though an ancestor could use it.
    opus = foundation_model_arn(
        "us.anthropic.claude-opus-4-1-20250805-v1:0", region=REGION, account=ACCOUNT
    )
    oss = foundation_model_arn("openai.gpt-oss-20b-1:0", region=REGION)
    assert _eval(iam_client, _GC, policy=mp, action="bedrock:Converse", resource=opus) in (
        "implicitDeny",
        "explicitDeny",
    )
    assert _eval(iam_client, _GC, policy=mp, action="bedrock:Converse", resource=oss) == "allowed"


@pytest.mark.aws
def test_grandchild_cross_tenant_denied(iam_client):
    dp = data_scope_policy(bucket=DOCS_BUCKET)
    d = _eval(
        iam_client,
        _GC,
        policy=dp,
        action="s3:GetObject",
        resource=f"arn:aws:s3:::{DOCS_BUCKET}/psych/chemistry/chem-101/wk.pdf",
    )
    assert d in ("implicitDeny", "explicitDeny")
