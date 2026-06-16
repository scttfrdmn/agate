"""Tool catalog proofs (#113 + #114): a declared gateway tool is invocable, an undeclared
action is denied, and an HPC submit is gated on the scope+budget cascade.

The budget-gate test is pure (reuses `cost.evaluate_cascade`, like the agent-graph #112).
The IAM test is live (`iam:SimulateCustomPolicy`, `-m aws`): the compiled tool policy
allows `InvokeGateway` on a declared tool's gateway ARN and denies it when the tenant tag
is absent — "a tool call can't widen reach; undeclared is denied", proven in STS.
"""

from __future__ import annotations

import json

import pytest
from agate.agentcompile import compile_agent
from agate.agentspec import parse_spec
from agate.names import tag_key
from cost import evaluate_cascade

REGION = "us-east-1"
ACCOUNT = "111122223333"
GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/agate-*"

boto3 = pytest.importorskip("boto3")


def _hpc_spec(tools):
    return parse_spec(
        {
            "agent": "lab-agent", "description": "d", "role": "researcher",
            "scope": "lab/photonics", "reasoning": "lit-review", "tools": tools,
        }
    )


# --- #114 allocation guard: a submit beyond the budget is rejected (pure) ---


def test_hpc_submit_rejected_when_over_allocation_budget():
    # The submit's worst-case cost must fit the lab's allocation budget (the #81 cascade,
    # reused). A node with only $0.000001 left rejects a costly submit, naming the node.
    nodes = [("lab/photonics", 0.0, 0.000001)]
    res = evaluate_cascade(
        model_id="us.anthropic.claude-opus-4-1-20250805-v1:0",
        input_tokens=100000, max_tokens=4000, nodes=nodes,
    )
    assert res.decision == "reject"
    assert res.breaching_node == "lab/photonics"


def test_hpc_submit_allowed_within_allocation():
    nodes = [("lab/photonics", 0.0, 100.0)]
    res = evaluate_cascade(
        model_id="openai.gpt-oss-20b-1:0", input_tokens=1000, max_tokens=500, nodes=nodes
    )
    assert res.decision == "allow"


# --- live IAM: declared gateway tool invocable, undeclared denied -----------


@pytest.fixture(scope="module")
def iam_client():
    client = boto3.client("iam", region_name=REGION)
    try:
        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable AWS credentials for live simulation: {exc}")
    return client


def _tags() -> list[dict]:
    return [
        {"ContextKeyName": f"aws:PrincipalTag/{tag_key('tenant')}", "ContextKeyType": "string",
         "ContextKeyValues": ["uni"]},
        {"ContextKeyName": f"aws:PrincipalTag/{tag_key('scope')}", "ContextKeyType": "string",
         "ContextKeyValues": ["lab/photonics"]},
    ]


def _eval(iam_client, policy, *, action, resource, tags) -> str:
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(policy)],
        ActionNames=[action],
        ResourceArns=[resource],
        ContextEntries=tags,
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


@pytest.mark.aws
def test_declared_gateway_tool_is_invocable(iam_client):
    c = compile_agent(_hpc_spec(["hpc-submit"]), region=REGION, account=ACCOUNT,
                      gateway_arn=GATEWAY_ARN)
    d = _eval(
        iam_client, c.tool_policy, action="bedrock-agentcore:InvokeGateway",
        resource="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/agate-hpc",
        tags=_tags(),
    )
    assert d == "allowed"


@pytest.mark.aws
def test_undeclared_tool_action_is_denied(iam_client):
    # The agent declared ONLY hpc-monitor; a different agentcore action is denied by absence.
    c = compile_agent(_hpc_spec(["hpc-monitor"]), region=REGION, account=ACCOUNT,
                      gateway_arn=GATEWAY_ARN)
    d = _eval(
        iam_client, c.tool_policy, action="bedrock-agentcore:CreateGateway",  # not granted
        resource="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/agate-hpc",
        tags=_tags(),
    )
    assert d in ("implicitDeny", "explicitDeny")


@pytest.mark.aws
def test_no_tenant_tag_denies_tool_invocation(iam_client):
    c = compile_agent(_hpc_spec(["hpc-submit"]), region=REGION, account=ACCOUNT,
                      gateway_arn=GATEWAY_ARN)
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(c.tool_policy)],
        ActionNames=["bedrock-agentcore:InvokeGateway"],
        ResourceArns=["arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/agate-hpc"],
        ContextEntries=[],  # no tenant tag -> the DenyToolsWhenNoTenantTag guard fires
    )
    assert resp["EvaluationResults"][0]["EvalDecision"] in ("implicitDeny", "explicitDeny")


# --- #119 Skills: a skill compiles to its capabilities' IAM, no more --------


def _skill_spec(skills):
    return parse_spec(
        {
            "agent": "lab-agent", "description": "d", "role": "researcher",
            "scope": "lab/photonics", "reasoning": "lit-review", "skills": skills,
        }
    )


@pytest.mark.aws
def test_skill_granted_gateway_tool_is_invocable(iam_client):
    # hpc-analyst bundles hpc-monitor + hpc-submit; a skills-only spec's compiled policy
    # invokes the gateway exactly as if the tools were listed directly.
    c = compile_agent(_skill_spec(["hpc-analyst"]), region=REGION, account=ACCOUNT,
                      gateway_arn=GATEWAY_ARN)
    d = _eval(
        iam_client, c.tool_policy, action="bedrock-agentcore:InvokeGateway",
        resource="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/agate-hpc",
        tags=_tags(),
    )
    assert d == "allowed"


@pytest.mark.aws
def test_skill_does_not_widen_beyond_its_capabilities(iam_client):
    # An agent whose ONLY skill is hpc-analyst still can't invoke an undeclared agentcore
    # action — the skill grants its bundle, never more.
    c = compile_agent(_skill_spec(["hpc-analyst"]), region=REGION, account=ACCOUNT,
                      gateway_arn=GATEWAY_ARN)
    d = _eval(
        iam_client, c.tool_policy, action="bedrock-agentcore:CreateGateway",  # not granted
        resource="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/agate-hpc",
        tags=_tags(),
    )
    assert d in ("implicitDeny", "explicitDeny")
