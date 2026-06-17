"""CDK synth assertions for the MemoryStack (§3, #110/#130). No deploy.

Asserts the opt-in memory resource synthesizes: a `CfnMemory` with the semantic + summary
strategies, the read/write Lambda, and the Lambda's execution role carrying the #110 ABAC
fence (`memory_access_policy` — the namespacePath Allow + the two Denies). Offline synth only.
"""

from __future__ import annotations

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402
from infra.stacks.memory import MemoryStack  # noqa: E402

_ENV = cdk.Environment(account="111122223333", region="us-east-1")


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    stack = MemoryStack(app, "agate-memory-synth", env=_ENV)
    return assertions.Template.from_stack(stack)


def test_memory_resource_with_both_strategies(template):
    mems = template.find_resources("AWS::BedrockAgentCore::Memory")
    assert len(mems) == 1
    props = list(mems.values())[0]["Properties"]
    strategies = props["MemoryStrategies"]
    # both built-in strategies present (semantic + summary; graph/temporal deferred)
    keys = set()
    for s in strategies:
        keys |= set(s.keys())
    assert "SemanticMemoryStrategy" in keys
    assert "SummaryMemoryStrategy" in keys
    # event expiry is set (default 90)
    assert props["EventExpiryDuration"] == 90


def test_memory_has_extraction_execution_role(template):
    # AgentCore assumes a role to run extraction; it must trust the service principal.
    mems = list(template.find_resources("AWS::BedrockAgentCore::Memory").values())[0]
    assert "MemoryExecutionRoleArn" in mems["Properties"]
    roles = template.find_resources("AWS::IAM::Role")
    trusts = [
        r["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"].get("Service")
        for r in roles.values()
    ]
    assert any("bedrock-agentcore" in str(t) for t in trusts)


def test_readwrite_lambda_wires_memory_id(template):
    fns = template.find_resources("AWS::Lambda::Function")
    memfn = [
        f
        for f in fns.values()
        if f["Properties"].get("Handler") == "infra.functions.memory.handler.handler"
    ]
    assert len(memfn) == 1
    env = memfn[0]["Properties"]["Environment"]["Variables"]
    assert "AGATE_MEMORY_ID" in env


def test_lambda_role_carries_the_abac_memory_fence(template):
    # The load-bearing #110 fence: the inline policy on the Lambda role must carry the
    # namespacePath Allow + the no-tenant Deny + the shared-outside-scope Deny.
    policies = template.find_resources("AWS::IAM::Policy")
    sids = []
    for p in policies.values():
        for stmt in p["Properties"]["PolicyDocument"]["Statement"]:
            if "Sid" in stmt:
                sids.append(stmt["Sid"])
    assert "AccessOwnTenantMemory" in sids
    assert "DenyMemoryWhenNoTenantTag" in sids
    assert "DenySharedMemoryOutsideScope" in sids


def test_cost_posture_output_is_marked_opt_in(template):
    template.has_output("CostPosture", {"Value": "billable-not-zero-idle-opt-in"})
