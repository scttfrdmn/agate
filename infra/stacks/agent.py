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
from agate.names import HANDLE, tag_key
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

# Supplied at deploy time once the reference-agent image is built + pushed.
PLACEHOLDER_IMAGE = "PLACEHOLDER_AGENT_CONTAINER_URI"
# The single tenant this gateway instance serves (its ARN joins the tenant-fenced family the
# #113 `_DEFAULT_GATEWAY_ARN` authorizes: `gateway/agate-{tenant}-*`). One gateway per tenant.
PLACEHOLDER_TENANT = "demo"


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

        # --- AgentCore Gateway + Slurm MCP server (#136) -------------------
        # The live integration surface for the #113/#114 tool catalog. The Gateway is the
        # thing an agent's `bedrock-agentcore:InvokeGateway` grant resolves to; the Slurm
        # Lambda is the MCP target behind hpc-submit/hpc-monitor (#114).
        tenant = self.node.try_get_context("gateway_tenant") or PLACEHOLDER_TENANT

        # The Slurm MCP server Lambda — the EFFECT half of §5. The pure scope→account map +
        # budget gate live in `agate.slurm`; this Lambda is the AWS edge (verify token, read
        # spend/budget, submit to the deploy-wired cluster). Mirrors the chokepoint's bundle.
        spend_table = self.node.try_get_context("spend_table") or f"{HANDLE}-spend"
        budget_table = self.node.try_get_context("budget_table") or f"{HANDLE}-budget"
        slurm_fn = lambda_.Function(
            self,
            "SlurmTool",
            function_name=f"{HANDLE}-slurm-tool",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="infra.functions.slurm.handler.handler",
            code=pip_bundled_code("agate", "infra", "cost", "meter"),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            environment={
                "AGATE_SPEND_TABLE": spend_table,
                "AGATE_BUDGET_TABLE": budget_table,
                # The verified-token coordinates (same Cognito the Runtime trusts inbound).
                "AGATE_OIDC_ISSUER": oidc_discovery_url or "",
                "AGATE_OIDC_AUDIENCE": allowed_audience or "",
            },
            description="agate Slurm MCP server - scope->allocation + budget-gated hpc-submit",
        )
        # Read the authoritative spend + budget tables (the cascade gate's inputs). No write:
        # the spend meter records the debit out-of-band, exactly as the chat path does.
        slurm_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadSpendAndBudget",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem", "dynamodb:Query"],
                resources=[
                    f"arn:aws:dynamodb:{region}:{account}:table/{spend_table}",
                    f"arn:aws:dynamodb:{region}:{account}:table/{budget_table}",
                ],
            )
        )

        # The Gateway — MCP protocol, custom-JWT inbound auth (the verified campus user, the
        # SAME discovery URL the Runtime uses). NAME is `agate-{tenant}` so its ARN joins the
        # tenant-fenced family `_DEFAULT_GATEWAY_ARN` already authorizes (#113): a live ARN
        # outside `gateway/agate-{tenant}-*` would miss every grant.
        gateway_name = f"{HANDLE}-{tenant}"
        gateway_authorizer = None
        if oidc_discovery_url:
            gateway_authorizer = agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=oidc_discovery_url,
                    allowed_audience=[allowed_audience] if allowed_audience else None,
                )
            )
        gateway = agentcore.CfnGateway(
            self,
            "ToolGateway",
            name=gateway_name,
            role_arn=execution_role.role_arn,
            authorizer_type="CUSTOM_JWT",
            protocol_type="MCP",
            authorizer_configuration=gateway_authorizer,
            description=f"agate campus-tool gateway (tenant {tenant}) - MCP, per-request",
        )

        # OAuth2 credential provider for USER-DELEGATED outbound auth (#136 / §5): the agent
        # reaches an external system AS the verified user, so the source ACL composes with
        # agate's scope. Slurm (an internal cluster) uses the scoped IAM role, not OAuth — so
        # the Slurm target below uses the IAM credential path; this provider is here for the
        # #133 connector targets (Drive/Box/Teams/Discord) to attach as they land.
        google_client_id = self.node.try_get_context("google_oauth_client_id")
        google_secret_arn = self.node.try_get_context("google_oauth_secret_arn")
        oauth_provider = None
        if google_client_id and google_secret_arn:
            oauth_provider = agentcore.CfnOAuth2CredentialProvider(
                self,
                "UserDelegatedOAuth",
                name=f"{HANDLE}-{tenant}-gdrive",
                credential_provider_vendor="GoogleOauth2",
                oauth2_provider_config_input=(
                    agentcore.CfnOAuth2CredentialProvider.Oauth2ProviderConfigInputProperty(
                        google_oauth2_provider_config=(
                            agentcore.CfnOAuth2CredentialProvider.GoogleOauth2ProviderConfigInputProperty(
                                client_id=google_client_id,
                                client_secret=(
                                    agentcore.CfnOAuth2CredentialProvider.ClientSecretArnProperty(
                                        secret_arn=google_secret_arn
                                    )
                                ),
                            )
                        )
                    )
                ),
            )

        # The Slurm MCP target — wraps the Lambda, declaring the two #114 tools as a typed
        # inline tool schema. Outbound auth is the gateway's own IAM identity (an internal
        # cluster), not user-delegated OAuth.
        _obj_schema = agentcore.CfnGatewayTarget.SchemaDefinitionProperty(type="object")

        def _tool(name, desc, props):  # -> ToolDefinitionProperty
            return agentcore.CfnGatewayTarget.ToolDefinitionProperty(
                name=name,
                description=desc,
                input_schema=agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
                    type="object", properties=props
                ),
            )

        slurm_target = agentcore.CfnGatewayTarget(
            self,
            "SlurmTarget",
            gateway_identifier=gateway.attr_gateway_identifier,
            name=f"{HANDLE}-slurm",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=slurm_fn.function_arn,
                        tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=[
                                _tool(
                                    "hpc-monitor",
                                    "Read the caller's own HPC jobs (read-only)",
                                    {},
                                ),
                                _tool(
                                    "hpc-submit",
                                    "Submit an HPC job to the caller's allocation (budget-gated)",
                                    {
                                        "job_spec": _obj_schema,
                                    },
                                ),
                            ]
                        ),
                    )
                )
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE",
                )
            ],
            description="agate Slurm MCP target (hpc-submit/hpc-monitor)",
        )
        slurm_target.add_dependency(gateway)
        # The Gateway invokes the Slurm Lambda; grant it on the function.
        slurm_fn.add_permission(
            "AllowGatewayInvoke",
            principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            action="lambda:InvokeFunction",
        )

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

        # Gateway + Slurm tool (#136).
        cdk.CfnOutput(self, "GatewayArn", value=gateway.attr_gateway_arn)
        cdk.CfnOutput(self, "GatewayId", value=gateway.attr_gateway_identifier)
        cdk.CfnOutput(self, "SlurmLambdaArn", value=slurm_fn.function_arn)
        cdk.CfnOutput(self, "SlurmTargetName", value=f"{HANDLE}-slurm")
        # Confirm the live gateway NAME joins the tenant-fenced ARN family the #113 grant
        # authorizes (`gateway/agate-{tenant}-*`). If this is ever false, every tool grant
        # misses and the agent can invoke nothing — fail-loud at deploy review.
        fenced = gateway_name.startswith(f"{HANDLE}-{tenant}")
        cdk.CfnOutput(
            self,
            "GatewayArnPatternStatus",
            value="tenant-fenced-ok" if fenced else "PATTERN-MISMATCH-grants-will-miss",
        )
        cdk.CfnOutput(
            self,
            "OutboundOAuthStatus",
            value="google-configured" if oauth_provider is not None else "PLACEHOLDER-no-oauth",
        )

        self.runtime = runtime
        self.execution_role = execution_role
        self.gateway = gateway
        self.slurm_fn = slurm_fn
        self.gateway_name = gateway_name
        # The tag-key constant is referenced by the synth test's fence assertion.
        self._tenant_tag_key = tag_key("tenant")
