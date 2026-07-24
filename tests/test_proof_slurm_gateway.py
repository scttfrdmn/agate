"""Live IAM proof (#136): the DEPLOYED-shape Slurm gateway ARN joins the #113 tenant fence.

#113 proved a declared gateway tool is invocable against a wildcard ARN. #136 deploys a
CONCRETE gateway named `agate-{tenant}` (see infra/stacks/agent.py), whose ARN is
`.../gateway/agate-{tenant}-*`. This proves, against live IAM (`SimulateCustomPolicy`, `-m
aws`), that the compiled `hpc-submit` tool policy ALLOWS `InvokeGateway` on the caller's OWN
tenant gateway and DENIES a different tenant's — so the live wiring can't widen the fence.

Run: `AWS_PROFILE=aws uv run pytest -m aws tests/test_proof_slurm_gateway.py`
"""

from __future__ import annotations

import json

import pytest
from agate.agentcompile import compile_agent
from agate.agentspec import parse_spec
from agate.names import tag_key

REGION = "us-east-1"
ACCOUNT = "111122223333"
# The tenant-fenced gateway ARN family the #113 grant authorizes (principal-tag interpolated).
GATEWAY_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/agate-"
    f"${{aws:PrincipalTag/{tag_key('tenant')}}}-*"
)
# Concrete deployed-shape ARNs (what `agate-{tenant}` synthesises to at deploy).
OWN_TENANT_GATEWAY = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/agate-uni-slurm"
OTHER_TENANT_GATEWAY = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/agate-other-slurm"

boto3 = pytest.importorskip("boto3")


def _hpc_spec(tools):
    return parse_spec(
        {
            "agent": "lab-agent",
            "description": "d",
            "role": "researcher",
            "scope": "lab/photonics",
            "reasoning": "lit-review",
            "tools": tools,
        }
    )


@pytest.fixture(scope="module")
def iam_client():
    client = boto3.client("iam", region_name=REGION)
    try:
        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable AWS credentials for live simulation: {exc}")
    return client


def _tags(tenant="uni") -> list[dict]:
    return [
        {
            "ContextKeyName": f"aws:PrincipalTag/{tag_key('tenant')}",
            "ContextKeyType": "string",
            "ContextKeyValues": [tenant],
        },
        {
            "ContextKeyName": f"aws:PrincipalTag/{tag_key('scope')}",
            "ContextKeyType": "string",
            "ContextKeyValues": ["lab/photonics"],
        },
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
def test_declared_hpc_submit_invokes_own_tenant_gateway(iam_client):
    c = compile_agent(
        _hpc_spec(["hpc-submit"]), region=REGION, account=ACCOUNT, gateway_arn=GATEWAY_ARN
    )
    d = _eval(
        iam_client,
        c.tool_policy,
        action="bedrock-agentcore:InvokeGateway",
        resource=OWN_TENANT_GATEWAY,
        tags=_tags("uni"),
    )
    assert d == "allowed"


@pytest.mark.aws
def test_cross_tenant_gateway_is_denied(iam_client):
    # A `uni` agent cannot invoke the `other` tenant's gateway — the principal-tag
    # interpolation fences the ARN to the caller's own tenant.
    c = compile_agent(
        _hpc_spec(["hpc-submit"]), region=REGION, account=ACCOUNT, gateway_arn=GATEWAY_ARN
    )
    d = _eval(
        iam_client,
        c.tool_policy,
        action="bedrock-agentcore:InvokeGateway",
        resource=OTHER_TENANT_GATEWAY,
        tags=_tags("uni"),
    )
    assert d in ("implicitDeny", "explicitDeny")


@pytest.mark.aws
def test_agent_with_no_tools_cannot_invoke_the_gateway(iam_client):
    # An agent that declared NO tools gets no InvokeGateway allow — denied by absence.
    c = compile_agent(_hpc_spec([]), region=REGION, account=ACCOUNT, gateway_arn=GATEWAY_ARN)
    d = _eval(
        iam_client,
        c.tool_policy,
        action="bedrock-agentcore:InvokeGateway",
        resource=OWN_TENANT_GATEWAY,
        tags=_tags("uni"),
    )
    assert d in ("implicitDeny", "explicitDeny")


@pytest.mark.aws
def test_no_tenant_tag_denies_invocation(iam_client):
    c = compile_agent(
        _hpc_spec(["hpc-submit"]), region=REGION, account=ACCOUNT, gateway_arn=GATEWAY_ARN
    )
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(c.tool_policy)],
        ActionNames=["bedrock-agentcore:InvokeGateway"],
        ResourceArns=[OWN_TENANT_GATEWAY],
        ContextEntries=[],  # no tenant tag → the DenyToolsWhenNoTenantTag guard fires
    )
    assert resp["EvaluationResults"][0]["EvalDecision"] in ("implicitDeny", "explicitDeny")
