"""Phase 8 — the agent path: AgentCore Runtime + Code Interpreter (design §13.7).

Hosts the academic-interaction-model orchestration (Panel/Analyze/router — built and
fakes-tested in `agate/panel`, `agate/analyze`, `agate/router`) on AgentCore Runtime, which
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
from agate.entitlements import model_arns_for_tier
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
            description="agate agent Runtime execution role: Bedrock invoke + tenant retrieval",
        )
        # The Runtime must pull the agent image from ECR at cold start. AgentCore
        # validates these on the execution role at create time. GetAuthorizationToken
        # is account-wide (no resource); the image-layer reads are scoped to the
        # agate-agent repo.
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrAuth",
                effect=iam.Effect.ALLOW,
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrPull",
                effect=iam.Effect.ALLOW,
                actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                resources=[f"arn:aws:ecr:{region}:{account}:repository/{HANDLE}-agent"],
            )
        )
        # The Runtime writes its own logs; AgentCore expects the execution role to
        # be able to create/write the agent's log group.
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="Logs",
                effect=iam.Effect.ALLOW,
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/*",
                ],
            )
        )

        # SEC-2: bound the role's Bedrock invoke to agate's entitled models only (the
        # full tier superset = frontier, cumulative). Per-SESSION tier enforcement is
        # done in the container against the verified JWT (model_arns_for_tier); this
        # IAM bound is the outer universe, not the per-user scope.
        model_resources = model_arns_for_tier("frontier", region=region, account=account)
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
                resources=model_resources,
            )
        )
        # SEC-2b: the agent does NOT retrieve — `evidence` is supplied in the
        # invocation payload (the SPA runs the scoped S3 Vectors query Tier-0-style
        # and passes the result). So the execution role is granted NO retrieval/data
        # permissions: there is no code path that reads tenant data, and a single
        # shared role couldn't scope it per-tenant anyway. If a retrieval TOOL is
        # later added to the agent, it MUST derive the tenant from the verified token
        # (agate.jwt_verify -> claims_to_tags) and scope the query to that tenant's
        # `agate-{tenant}` index — and only then is a correspondingly-scoped grant added
        # here. Keeping the grant off until the code exists is least-privilege and
        # closes the latent cross-tenant read the review flagged.

        # --- Code Interpreter (Analyze microVM) ---------------------------
        # PUBLIC network (no VPC) — sandboxed code execution, scales to zero.
        code_interpreter = agentcore.CfnCodeInterpreterCustom(
            self,
            "CodeInterpreter",
            name=f"{HANDLE}_code_interpreter",
            description="agate Analyze sandbox (Code Interpreter microVM)",
            execution_role_arn=execution_role.role_arn,
            network_configuration=agentcore.CfnCodeInterpreterCustom.CodeInterpreterNetworkConfigurationProperty(
                network_mode="PUBLIC",
            ),
        )

        # The Analyze path runs generated code in the Code Interpreter — the agent's
        # OWN execution role makes that data-plane call, so it needs invoke on this
        # interpreter (Ask/Panel never touch it, which is why they worked without
        # this grant). Scoped to the agate code interpreters in this account/region.
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CodeInterpreterInvoke",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock-agentcore:InvokeCodeInterpreter",
                    "bedrock-agentcore:StartCodeInterpreterSession",
                    "bedrock-agentcore:StopCodeInterpreterSession",
                    "bedrock-agentcore:GetCodeInterpreterSession",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{region}:{account}:code-interpreter-custom/{HANDLE}_code_interpreter-*",
                ],
            )
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
                # both are non-secret. The orchestration (agate.analyze) invokes the
                # Code Interpreter by this id.
                "AGATE_REGION": region,
                "AGATE_CODE_INTERPRETER_ID": code_interpreter.attr_code_interpreter_id,
            },
            description="agate agent path - hosts Panel/Analyze/router orchestration",
        )
        runtime.add_dependency(code_interpreter)

        # AgentCore validates the execution role's ECR permissions at Runtime-create
        # time. CloudFormation otherwise creates the Runtime as soon as the Role
        # *resource* exists — before its inline policy (the separate DefaultPolicy
        # node carrying the ECR grants) is attached — so creation races and fails
        # "Access denied while validating ECR URI". Force both the Runtime and the
        # Code Interpreter to wait for the role's default policy.
        role_policy = execution_role.node.try_find_child("DefaultPolicy")
        if role_policy is not None:
            policy_resource = role_policy.node.default_child
            runtime.add_dependency(policy_resource)
            code_interpreter.add_dependency(policy_resource)

        # A named endpoint the SPA's agentcore transport invokes. Pin it to the
        # Runtime's CURRENT version: pushing a new image bumps the Runtime version,
        # and without this the `default` endpoint keeps serving the OLD version
        # (symptom: a stale container, or HTTP 424 if the new image differs) until
        # repointed by hand. Binding the endpoint to `attr_agent_runtime_version`
        # makes every `cdk deploy` that changes the image also roll the endpoint.
        endpoint = agentcore.CfnRuntimeEndpoint(
            self,
            "RuntimeEndpoint",
            agent_runtime_id=runtime.attr_agent_runtime_id,
            agent_runtime_version=runtime.attr_agent_runtime_version,
            name="default",
            description="agate agent Runtime default endpoint",
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
