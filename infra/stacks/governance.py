"""Phase 5 governance tail (design §5, §8, §13.4) — Guardrails + AgentCore Policy.

Two machine-checkable governance layers, both per-use/no-clock:
  * a **Bedrock Guardrail** (content + sensitive-information filters) a CISO can read
    and that applies to model calls (Tier 0 per-role or Tier 1 centralized, §8).
  * an **AgentCore PolicyEngine** that hosts the **agent-path tool-authz** Cedar
    policy set (`policy.cedar.agentcore_tool_policy_statements`) — native, per-tool
    authorization enforced on every Gateway tool call, sitting UNDER the #113 IAM
    gateway fence as defence in depth (§8).

#154: AgentCore Policy uses its OWN Cedar schema — each policy must name a concrete
`AgentCore::Gateway` ARN, a tool action (`AgentCore::Action::"<target>___<tool>"`),
an authenticated principal type, and a constraining `when` (confirmed live; a bare or
abstract policy is rejected). The chat-path IAM mirror in `policy.cedar`
(`policy_statements()`) is therefore NOT deployed here — IAM (#5/#13.2) enforces the
chat path; that Cedar is the human-auditable mirror only.

Because the governing Gateway is owned by the agent stack, its ARN isn't known at
synth in isolation. The tool policies are loaded only when a `governance_gateway_arn`
context value is supplied (the deployed Gateway ARN); without it the stack ships the
Guardrail + the (empty) PolicyEngine — both deploy clean — and the tool policies are
added by re-running the deploy with the ARN once the agent stack is live.

L1 `Cfn*` (no L2 for Guardrail or AgentCore Policy yet; migration tracked in #22).
NO CLOCKS: Guardrails bill per-use; the Policy engine is config, not a running box.
"""

from __future__ import annotations

import aws_cdk as cdk
from agate.names import HANDLE
from aws_cdk import (
    Stack,
)
from aws_cdk import (
    aws_bedrock as bedrock,
)
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
)
from constructs import Construct
from policy.cedar import agentcore_tool_policy_statements


class GovernanceStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Bedrock Guardrail (§8 responsible-AI safeguards) -------------
        # A baseline content filter + PII handling a CISO can read. Strengths are
        # conservative defaults; institutions tune per policy. Applied to model
        # calls via the guardrailIdentifier on Converse (Tier 0 per-role / Tier 1).
        guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=f"{HANDLE}-baseline",
            description="agate baseline guardrail - content + PII filters (CISO-readable)",
            blocked_input_messaging="This request was blocked by the agate guardrail.",
            blocked_outputs_messaging="This response was blocked by the agate guardrail.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=t, input_strength="HIGH", output_strength="HIGH"
                    )
                    for t in ("SEXUAL", "VIOLENCE", "HATE", "INSULTS", "MISCONDUCT")
                ]
                + [
                    # Prompt-attack filtering applies to input only (per service rules).
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE"
                    )
                ],
            ),
            sensitive_information_policy_config=(
                bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                    pii_entities_config=[
                        bedrock.CfnGuardrail.PiiEntityConfigProperty(type=t, action="ANONYMIZE")
                        for t in ("EMAIL", "PHONE", "NAME", "US_SOCIAL_SECURITY_NUMBER")
                    ]
                )
            ),
        )

        # --- AgentCore PolicyEngine (Cedar, §13.4) ------------------------
        # The engine hosts the agent-path tool-authz policies. It deploys cleanly with NO
        # policies (a Gateway attaches to it via update-gateway's policy-engine-configuration).
        engine = agentcore.CfnPolicyEngine(
            self,
            "PolicyEngine",
            name=f"{HANDLE}_policy_engine",
            description="agate AgentCore policy engine - Gateway tool authz (Cedar, #154)",
        )

        # The tool policies need a CONCRETE Gateway ARN (AgentCore rejects an abstract resource
        # — the #154 deploy blocker — and rejects a non-ARN gateway id). The Gateway is owned by
        # the agent stack, so its ARN comes in as context once that stack is live. Without it,
        # ship just the Guardrail + (empty) engine; with it, load one permit per tool.
        gateway_arn = self.node.try_get_context("governance_gateway_arn")
        # The Slurm target's tools (#114) and target name — mirror infra/stacks/agent.py.
        slurm_target = f"{HANDLE}-slurm"
        slurm_tools = ["hpc-submit", "hpc-monitor"]
        tool_policy_count = 0
        if gateway_arn:
            # Each AgentCore `CfnPolicy` holds exactly ONE Cedar statement, so the set loads as N
            # policies under the engine — one per Gateway tool, each pinned to this Gateway ARN +
            # the tool action + a constraining `when` (a bare or abstract permit is rejected by
            # the analyzer; confirmed live, #154). Generated from the deployed tool list.
            for stmt_name, statement in agentcore_tool_policy_statements(
                gateway_arn, slurm_tools, slurm_target
            ):
                cid = "CedarPolicy" + "".join(p.capitalize() for p in stmt_name.split("-"))
                policy = agentcore.CfnPolicy(
                    self,
                    cid,
                    name=f"{HANDLE}_{stmt_name.replace('-', '_')}",
                    policy_engine_id=engine.attr_policy_engine_id,
                    # Strict by default: we WANT the Cedar analyzer to pass, not be bypassed.
                    validation_mode="FAIL_ON_ANY_FINDINGS",
                    definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                        cedar=agentcore.CfnPolicy.CedarPolicyProperty(statement=statement),
                    ),
                    description=(
                        "Agent-path tool authz (AgentCore Cedar schema) - "
                        "defence in depth under the #113 IAM gateway fence"
                    ),
                )
                policy.add_dependency(engine)
                tool_policy_count += 1

        # --- Outputs -------------------------------------------------------
        cdk.CfnOutput(self, "GuardrailId", value=guardrail.attr_guardrail_id)
        cdk.CfnOutput(self, "GuardrailArn", value=guardrail.attr_guardrail_arn)
        cdk.CfnOutput(self, "PolicyEngineId", value=engine.attr_policy_engine_id)
        cdk.CfnOutput(
            self,
            "ToolPolicyStatus",
            value=(
                f"{tool_policy_count}-tool-policies"
                if gateway_arn
                else "no-gateway-arn-engine-only"
            ),
        )

        self.guardrail = guardrail
        self.policy_engine = engine
