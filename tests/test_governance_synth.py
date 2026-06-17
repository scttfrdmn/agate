"""CDK synth assertions for the GovernanceStack (§5/§8/§13.4, #154). No deploy.

The #154 fix: AgentCore Policy uses its OWN Cedar schema (a policy must name a concrete
`AgentCore::Gateway` ARN, a tool action, an authenticated principal type, and a constraining
`when` — confirmed live). The abstract IAM-mirror Cedar that previously loaded here is rejected
by the AgentCore analyzer, which is why the stack failed to deploy. So:

  * the Guardrail + (empty) PolicyEngine always synthesize (and deploy clean);
  * the agent-path tool policies appear ONLY when a `governance_gateway_arn` context value is
    supplied (the deployed Gateway's ARN), and when they do they carry the AgentCore schema —
    a specific gateway resource, NOT the abstract `resource`/`InvokeModel` mirror.

Synth is offline (no AWS creds, no deploy); it exercises the CDK construct graph only.
"""

from __future__ import annotations

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402
from infra.stacks.governance import GovernanceStack  # noqa: E402

_ENV = cdk.Environment(account="111122223333", region="us-east-1")
_GW_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/agate-demo-abc123"


def _template(context=None):
    app = cdk.App(context=context or {})
    stack = GovernanceStack(app, "agate-governance-synth", env=_ENV)
    return assertions.Template.from_stack(stack)


def test_guardrail_and_engine_synthesize_without_gateway_arn():
    # The fallback / always-deployable path: no gateway ARN -> Guardrail + empty engine only.
    t = _template()
    assert len(t.find_resources("AWS::Bedrock::Guardrail")) == 1
    assert len(t.find_resources("AWS::BedrockAgentCore::PolicyEngine")) == 1
    # No tool policies without a concrete gateway ARN (an abstract policy is the #154 blocker).
    assert len(t.find_resources("AWS::BedrockAgentCore::Policy")) == 0


def test_guardrail_filters_content_and_pii():
    t = _template()
    gr = list(t.find_resources("AWS::Bedrock::Guardrail").values())[0]["Properties"]
    filt = {f["Type"] for f in gr["ContentPolicyConfig"]["FiltersConfig"]}
    assert {"SEXUAL", "VIOLENCE", "HATE", "PROMPT_ATTACK"} <= filt
    pii = {p["Type"] for p in gr["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]}
    assert "US_SOCIAL_SECURITY_NUMBER" in pii and "EMAIL" in pii


def test_tool_policies_synthesize_when_gateway_arn_supplied():
    # With the deployed Gateway ARN in context, one AgentCore policy per Slurm tool loads.
    t = _template({"governance_gateway_arn": _GW_ARN})
    policies = t.find_resources("AWS::BedrockAgentCore::Policy")
    assert len(policies) == 2  # hpc-submit + hpc-monitor


def test_tool_policies_use_agentcore_schema_not_abstract_resource():
    # The load-bearing #154 assertion: deployed statements pin the specific gateway ARN +
    # tool action + authenticated principal + a constraining `when` — never the abstract
    # `resource`/`InvokeModel` mirror that the AgentCore analyzer rejects.
    t = _template({"governance_gateway_arn": _GW_ARN})
    policies = t.find_resources("AWS::BedrockAgentCore::Policy").values()
    statements = [p["Properties"]["Definition"]["Cedar"]["Statement"] for p in policies]
    blob = "\n".join(statements)
    assert f'AgentCore::Gateway::"{_GW_ARN}"' in blob
    assert 'AgentCore::Action::"agate-slurm___hpc-submit"' in blob
    assert "principal is AgentCore::IamEntity" in blob
    assert "when {" in blob and "principal has id" in blob
    # the abstract chat-path mirror must NOT be here
    assert 'Action::"InvokeModel"' not in blob
    assert "resource.tier" not in blob
    # every policy validates strictly (analyzer must pass, not be ignored)
    for p in policies:
        assert p["Properties"]["ValidationMode"] == "FAIL_ON_ANY_FINDINGS"
