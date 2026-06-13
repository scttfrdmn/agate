"""Phase 5 governance tail (design §5, §8, §13.4) — Guardrails + AgentCore Policy.

Two machine-checkable governance layers, both per-use/no-clock:
  * a **Bedrock Guardrail** (content + sensitive-information filters) a CISO can read
    and that applies to model calls (Tier 0 per-role or Tier 1 centralized, §8).
  * an **AgentCore Policy** (Cedar) loaded with the policy set GENERATED from the
    same `agg.entitlements` table as the IAM scope (`policy.cedar`) — so the
    human-auditable layer and the enforced layer cannot drift (design §5). The
    Policy is enforced natively on every agent tool/action call.

L1 `Cfn*` (no L2 for Guardrail or AgentCore Policy yet; migration tracked in #22).
NO CLOCKS: Guardrails bill per-use; the Policy engine is config, not a running box.
"""

from __future__ import annotations

import aws_cdk as cdk
from agg.names import HANDLE
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
from policy.cedar import generate_policy_set


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
            description="agg baseline guardrail — content + PII filters (CISO-readable)",
            blocked_input_messaging="This request was blocked by the agg guardrail.",
            blocked_outputs_messaging="This response was blocked by the agg guardrail.",
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

        # --- AgentCore Policy (Cedar, §13.4) ------------------------------
        # The policy engine hosts the Cedar policy set generated from the entitlement
        # table — the same source the IAM model-access policy uses.
        engine = agentcore.CfnPolicyEngine(
            self,
            "PolicyEngine",
            name=f"{HANDLE}_policy_engine",
            description="agg AgentCore policy engine — tool/action authz (Cedar)",
        )
        policy = agentcore.CfnPolicy(
            self,
            "CedarPolicy",
            name=f"{HANDLE}_entitlements",
            policy_engine_id=engine.attr_policy_engine_id,
            definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                cedar=agentcore.CfnPolicy.CedarPolicyProperty(
                    statement=generate_policy_set(),
                ),
            ),
            description="Generated from agg.entitlements — mirrors the IAM model scope",
        )
        policy.add_dependency(engine)

        # --- Outputs -------------------------------------------------------
        cdk.CfnOutput(self, "GuardrailId", value=guardrail.attr_guardrail_id)
        cdk.CfnOutput(self, "GuardrailArn", value=guardrail.attr_guardrail_arn)
        cdk.CfnOutput(self, "PolicyEngineId", value=engine.attr_policy_engine_id)

        self.guardrail = guardrail
        self.policy_engine = engine
