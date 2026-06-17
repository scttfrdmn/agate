"""Phase 7 — OPTIONAL AgentCore Memory (design §3, vision §3, #130).

The live home of cross-session memory: an AgentCore Memory resource (semantic +
summarization strategies) plus a read/write Lambda that records/recalls turns USING ONLY
`agate.memory.namespaces_for` — the namespace is always derived from the verified identity,
never the client (the #110 boundary). The Lambda's execution role carries
`policy.generate.memory_access_policy`, so the SAME `agate:` tag fence that guards documents
(#80) and vectors (#84) guards memory: no leak across tenant, principal, or scope (§10.3).

**COST POSTURE — this stack is OPT-IN.** Unlike every other agate resource (per-request /
storage-priced, $0 idle), managed AgentCore Memory is NOT $0-idle: it stores records and runs
extraction/summarization continuously. So the default fleet never deploys it — an institution
stands it up explicitly (`cdk deploy agate-memory`), exactly like the Tier-1 chokepoint and the
CloudTrail audit trail. NO CLOCKS stays true for the default deployment.

Cross-stack inputs (OIDC issuer/audience, event-expiry days) are passed via context so the
stack stays deployable on its own. The in-container runtime record/recall hook is a separate
follow-up (#130b) — it needs a container rebuild; this stack ships the resource + SDK path.
"""

from __future__ import annotations

import aws_cdk as cdk
from agate.names import HANDLE
from aws_cdk import (
    Stack,
)
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from constructs import Construct
from infra.assets import pip_bundled_code
from policy.generate import memory_access_policy


class MemoryStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Deploy-time config (supply at deploy with -c). Sensible defaults so the stack
        # synthesizes standalone.
        oidc_discovery_url = self.node.try_get_context("cognito_discovery_url") or ""
        allowed_audience = self.node.try_get_context("cognito_audience") or ""
        expiry_days = int(self.node.try_get_context("memory_expiry_days") or 90)

        # --- Extraction execution role -----------------------------------
        # AgentCore assumes this role to run the semantic/summary extraction strategies (it
        # invokes a Bedrock model to distil events into records). Minimal: the service
        # principal may assume it; it may invoke models for extraction. It is NOT the
        # read/write path's role (that's the Lambda's, below, which carries the ABAC fence).
        extraction_role = iam.Role(
            self,
            "MemoryExtractionRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="AgentCore Memory extraction role - runs semantic/summary strategies",
        )
        extraction_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeExtractionModels",
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                resources=[f"arn:aws:bedrock:{self.region}::foundation-model/*"],
            )
        )

        # --- AgentCore Memory resource -----------------------------------
        # Two built-in strategies per the 2026 research: SEMANTIC (fact/entity extraction) +
        # SUMMARY (rolling conversation summarization). Graph/temporal/episodic deferred (start
        # simple). Namespaces are NOT templated on the strategy: the read/write path targets the
        # concrete `namespaces_for` paths and the IAM `namespacePath` condition fences
        # tenant/scope per-credential at access time (the #110 live proof) — a single resource
        # serves the deployment.
        memory = agentcore.CfnMemory(
            self,
            "Memory",
            name=f"{HANDLE}_memory",
            event_expiry_duration=expiry_days,
            description="agate cross-session memory - 3-tier, ABAC-namespaced (#110/#130)",
            memory_strategies=[
                agentcore.CfnMemory.MemoryStrategyProperty(
                    semantic_memory_strategy=agentcore.CfnMemory.SemanticMemoryStrategyProperty(
                        name=f"{HANDLE}_semantic",
                        description="Fact/entity extraction across a principal's sessions",
                    ),
                ),
                agentcore.CfnMemory.MemoryStrategyProperty(
                    summary_memory_strategy=agentcore.CfnMemory.SummaryMemoryStrategyProperty(
                        name=f"{HANDLE}_summary",
                        description="Rolling conversation summarization per session",
                    ),
                ),
            ],
            memory_execution_role_arn=extraction_role.role_arn,
        )

        # --- Read/write Lambda (the SDK path, ABAC-fenced) ----------------
        # Records/recalls via the bedrock-agentcore SDK, deriving every namespace from
        # `agate.memory.namespaces_for` — never a client value. Mirrors the slurm MCP bundle.
        memory_fn = lambda_.Function(
            self,
            "MemoryTool",
            function_name=f"{HANDLE}-memory-tool",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="infra.functions.memory.handler.handler",
            code=pip_bundled_code("agate", "infra", "policy"),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            environment={
                "AGATE_MEMORY_ID": memory.attr_memory_id,
                "AGATE_REGION": self.region,
                # The verified-token coordinates (same Cognito the broker/Runtime trust).
                "AGATE_OIDC_ISSUER": oidc_discovery_url,
                "AGATE_OIDC_AUDIENCE": allowed_audience,
            },
            description="agate Memory read/write server - namespaces_for-fenced (#110/#130)",
        )
        # The #110 ABAC fence, scoped to THIS memory's ARN: read+write only under
        # `agate/{tenant}/...`, deny without a tenant tag, deny shared outside scope. The same
        # policy the live SimulateCustomPolicy proof validates.
        memory_fn.role.attach_inline_policy(
            iam.Policy(
                self,
                "MemoryAccess",
                document=iam.PolicyDocument.from_json(
                    memory_access_policy(memory_arn=memory.attr_memory_arn)
                ),
            )
        )

        # --- Outputs -------------------------------------------------------
        cdk.CfnOutput(self, "MemoryId", value=memory.attr_memory_id)
        cdk.CfnOutput(self, "MemoryArn", value=memory.attr_memory_arn)
        cdk.CfnOutput(self, "MemoryToolArn", value=memory_fn.function_arn)
        cdk.CfnOutput(
            self,
            "CostPosture",
            value="billable-not-zero-idle-opt-in",
        )

        self.memory = memory
        self.memory_fn = memory_fn
