"""Phase 8 — the agent path: AgentCore Runtime + Code Interpreter (design §13.7).

Hosts the academic-interaction-model orchestration (Panel/Analyze/router — built and
fakes-tested in `agg/panel`, `agg/analyze`, `agg/router`) on AgentCore Runtime, which
is serverless and **scales to zero** (NO CLOCKS). The reference agent is a
framework-agnostic container that honours the AgentCore invocation protocol; its
image is built and pushed out-of-band, so `container_uri` is deploy-time config.

Identity (security memo §6.1): **inbound** auth is the campus user via Cognito —
the Runtime's `custom_jwt_authorizer` validates the identity-pool JWT, so the
user's identity flows into the session. **Outbound** auth is the Runtime's own
execution role, scoped to Bedrock invoke + the tenant's S3 Vectors read — the same
boundary the chat path uses, expressed around the agent rather than inside it.

NO CLOCKS: `network_mode=PUBLIC` (no VPC — §14 non-goal: AgentCore VPC egress +
PrivateLink are clocks); Runtime + Code Interpreter are per-session microVMs that
return to zero. No standing component.

S3 Vectors / Bedrock KB / AgentCore have L1 `Cfn*` only (migration tracked in #22).
"""

from __future__ import annotations

import aws_cdk as cdk
from agg.names import HANDLE
from aws_cdk import (
    Stack,
)
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
)
from aws_cdk import (
    aws_iam as iam,
)
from constructs import Construct

# Supplied at deploy time once the reference-agent image is built + pushed.
PLACEHOLDER_IMAGE = "PLACEHOLDER_AGENT_CONTAINER_URI"


class AgentStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = self.region
        account = self.account

        # Deploy-time config (context): the agent image and the Cognito inbound-auth
        # coordinates. The OIDC discovery URL + audience identify the identity pool /
        # app client whose JWT the Runtime accepts as the inbound user identity.
        container_uri = self.node.try_get_context("agent_container_uri") or PLACEHOLDER_IMAGE
        oidc_discovery_url = self.node.try_get_context("cognito_discovery_url")
        allowed_audience = self.node.try_get_context("cognito_audience")

        # --- Runtime execution role (OUTBOUND tool identity) --------------
        # The agent's own role: invoke Bedrock models + read tenant S3 Vectors and
        # docs. Deny-by-default elsewhere. This is the outbound scope; the inbound
        # user identity arrives via the JWT authorizer, not this role.
        execution_role = iam.Role(
            self,
            "RuntimeExecutionRole",
            role_name=f"{HANDLE}-agent-runtime",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="agg agent Runtime execution role — Bedrock invoke + tenant retrieval",
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInvoke",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=["*"],
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="TenantRetrieval",
                effect=iam.Effect.ALLOW,
                actions=["s3vectors:QueryVectors", "s3vectors:GetVectors", "s3:GetObject"],
                resources=["*"],
            )
        )

        # --- Code Interpreter (Analyze microVM) ---------------------------
        # PUBLIC network (no VPC) — sandboxed code execution, scales to zero.
        code_interpreter = agentcore.CfnCodeInterpreterCustom(
            self,
            "CodeInterpreter",
            name=f"{HANDLE}_code_interpreter",
            description="agg Analyze sandbox (Code Interpreter microVM)",
            execution_role_arn=execution_role.role_arn,
            network_configuration=agentcore.CfnCodeInterpreterCustom.CodeInterpreterNetworkConfigurationProperty(
                network_mode="PUBLIC",
            ),
        )

        # --- AgentCore Runtime --------------------------------------------
        authorizer = None
        if oidc_discovery_url:
            authorizer = agentcore.CfnRuntime.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=oidc_discovery_url,
                    allowed_audience=[allowed_audience] if allowed_audience else None,
                )
            )

        runtime = agentcore.CfnRuntime(
            self,
            "Runtime",
            agent_runtime_name=f"{HANDLE}_agent",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=container_uri,
                ),
            ),
            role_arn=execution_role.role_arn,
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC",  # no VPC — NO CLOCKS (§14)
            ),
            authorizer_configuration=authorizer,
            environment_variables={
                # The agent reads its region + the Code Interpreter id at runtime;
                # both are non-secret. The orchestration (agg.analyze) invokes the
                # Code Interpreter by this id.
                "AGG_REGION": region,
                "AGG_CODE_INTERPRETER_ID": code_interpreter.attr_code_interpreter_id,
            },
            description="agg agent path — hosts Panel/Analyze/router orchestration",
        )
        runtime.add_dependency(code_interpreter)

        # A named endpoint the SPA's agentcore transport invokes.
        endpoint = agentcore.CfnRuntimeEndpoint(
            self,
            "RuntimeEndpoint",
            agent_runtime_id=runtime.attr_agent_runtime_id,
            name="default",
            description="agg agent Runtime default endpoint",
        )
        endpoint.add_dependency(runtime)

        # --- Outputs -------------------------------------------------------
        cdk.CfnOutput(self, "RuntimeArn", value=runtime.attr_agent_runtime_arn)
        cdk.CfnOutput(self, "RuntimeEndpointName", value="default")
        cdk.CfnOutput(self, "ExecutionRoleArn", value=execution_role.role_arn)
        cdk.CfnOutput(
            self,
            "AgentImageStatus",
            value="configured" if container_uri != PLACEHOLDER_IMAGE else PLACEHOLDER_IMAGE,
        )
        cdk.CfnOutput(
            self,
            "InboundAuthStatus",
            value="cognito-jwt" if oidc_discovery_url else "PLACEHOLDER-no-idp-wired",
        )
        # Note the account/region the agent path runs in (used in follow-up wiring).
        cdk.CfnOutput(self, "AgentAccountRegion", value=f"{account}/{region}")

        self.runtime = runtime
        self.execution_role = execution_role
