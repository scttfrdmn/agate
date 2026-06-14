"""Phase 1 — the identity broker + ABAC stack (design §13.1/§13.2, the crux).

What this stack stands up (all $0 idle — NO CLOCKS):
  * a Cognito **Identity Pool** federated to the campus IdP (SAML/OIDC). No User
    Pool — we hook into existing campus federation and avoid MAU cost (design §5).
  * the **authenticated IAM role**, deny-by-default, whose effective scope is the
    generated model-access + data-scope policies keyed on `agate:` principal tags.
  * a **permissions boundary** that hard-caps the role to Bedrock+S3+S3Vectors read
    surfaces, so no future policy edit can widen it past the ABAC intent.
  * the per-request **broker Lambda** that validates the IdP token, derives the four
    `agate:` tags (incl. the computed `agate:tier`), and assumes the authenticated role
    narrowed by them.

The model->tier map is GENERATED from agate.entitlements (single source of truth),
never written inline here.
"""

from __future__ import annotations

import aws_cdk as cdk
from agate.names import HANDLE, tag_key
from aws_cdk import (
    Stack,
)
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
)
from aws_cdk import (
    aws_apigatewayv2_integrations as apigwv2_integrations,
)
from aws_cdk import (
    aws_cognito_identitypool as idpool,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from constructs import Construct
from infra.assets import pip_bundled_code
from policy.generate import data_scope_policy, model_access_policy

# Sentinel for federation config that must be supplied before deploy.
PLACEHOLDER = "PLACEHOLDER"


class IdentityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = self.region
        account = self.account

        # --- Permissions boundary -----------------------------------------
        # Hard ceiling created first so the role can reference it. Even a future
        # mis-edit of the role cannot grant anything beyond these services; the
        # ABAC tags narrow WITHIN this ceiling.
        boundary = iam.ManagedPolicy(
            self,
            "AuthenticatedBoundary",
            managed_policy_name=f"{HANDLE}-authenticated-boundary",
            document=iam.PolicyDocument.from_json(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "CeilingBedrockInvoke",
                            "Effect": "Allow",
                            "Action": [
                                "bedrock:InvokeModel",
                                "bedrock:InvokeModelWithResponseStream",
                                "bedrock:Converse",
                                "bedrock:ConverseStream",
                            ],
                            "Resource": "*",
                        },
                        {
                            "Sid": "CeilingDataRead",
                            "Effect": "Allow",
                            "Action": [
                                "s3:GetObject",
                                "s3:ListBucket",
                                "s3vectors:QueryVectors",
                                "s3vectors:GetVectors",
                            ],
                            "Resource": "*",
                        },
                        {
                            # The agent path (Panel/Analyze) — the SPA SigV4-signs the
                            # InvokeAgentRuntime call with these vended creds. Inside
                            # the runtime, the container re-derives the caller's tier
                            # from the verified JWT (SEC-4b), so this only gates "may
                            # invoke the agent at all", not which models run.
                            "Sid": "CeilingAgentInvoke",
                            "Effect": "Allow",
                            "Action": "bedrock-agentcore:InvokeAgentRuntime",
                            "Resource": "*",
                        },
                        {
                            # Explicit deny of anything that could widen privilege
                            # or persist beyond the session.
                            "Sid": "CeilingDenyEscalation",
                            "Effect": "Deny",
                            "Action": [
                                "iam:*",
                                "sts:*",
                                "bedrock:CreateProvisionedModelThroughput",
                                "s3:PutObject",
                                "s3:DeleteObject",
                            ],
                            "Resource": "*",
                        },
                    ],
                }
            ),
        )

        # --- Authenticated role -------------------------------------------
        # Trusted by the broker Lambda's execution role (sts:AssumeRole with
        # session Tags). The Cognito Identity Pool also lists it as the
        # authenticated role for the (future) browser-direct refresh path.
        authenticated_role = iam.Role(
            self,
            "AuthenticatedRole",
            role_name=f"{HANDLE}-authenticated",
            assumed_by=iam.CompositePrincipal(
                # Cognito-federated principals (browser-direct refresh path).
                iam.FederatedPrincipal(
                    "cognito-identity.amazonaws.com",
                    conditions={
                        "StringEquals": {
                            "cognito-identity.amazonaws.com:aud": cdk.Token.as_string(
                                self.node.try_get_context("identity_pool_id") or PLACEHOLDER
                            )
                        },
                        "ForAnyValue:StringLike": {
                            "cognito-identity.amazonaws.com:amr": "authenticated"
                        },
                    },
                    assume_role_action="sts:AssumeRoleWithWebIdentity",
                ),
            ),
            description="agate: authenticated session role, narrowed by agate: ABAC session tags",
            max_session_duration=cdk.Duration.hours(1),
            permissions_boundary=boundary,
        )

        # The broker assumes this role WITH session tags, so the trust policy
        # must also allow sts:AssumeRole + sts:TagSession from the broker role.
        # (Added after the broker role exists, below.)

        # --- Generated ABAC policies (single source of truth) -------------
        model_policy_doc = model_access_policy(region=region, account=account)
        data_policy_doc = data_scope_policy()

        authenticated_role.attach_inline_policy(
            iam.Policy(
                self,
                "ModelAccess",
                document=iam.PolicyDocument.from_json(model_policy_doc),
            )
        )
        authenticated_role.attach_inline_policy(
            iam.Policy(
                self,
                "DataScope",
                document=iam.PolicyDocument.from_json(data_policy_doc),
            )
        )
        # Agent path: let the session invoke the agate AgentCore Runtime (Panel/Analyze).
        # Scoped to this account/region's agate agent runtimes by ARN pattern — the
        # runtime's generated id lives in the agate-agent stack, so we match the family
        # rather than create a cross-stack dependency. Per-tier model enforcement still
        # happens inside the container against the verified JWT (SEC-4b); this is just
        # the "may invoke" grant. Bounded by CeilingAgentInvoke above.
        authenticated_role.attach_inline_policy(
            iam.Policy(
                self,
                "AgentInvoke",
                document=iam.PolicyDocument.from_json(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "InvokeAgateAgentRuntime",
                                "Effect": "Allow",
                                "Action": "bedrock-agentcore:InvokeAgentRuntime",
                                "Resource": [
                                    f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{HANDLE}_agent-*",
                                    f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{HANDLE}_agent-*/*",
                                ],
                            }
                        ],
                    }
                ),
            )
        )

        # --- Broker OIDC verification config -------------------------------
        # The broker verifies the inbound IdP token against a JWKS (SEC-4). Supply
        # the OIDC issuer/JWKS/audience as deploy-time context — the SAME keys work
        # for a real campus IdP and for the throwaway demo pool (`agate-demo-idp`
        # outputs OidcIssuer/OidcJwksUrl/OidcAudience). Codified here so the demo is
        # reproducible without a post-deploy `aws lambda update-function-configuration`.
        # Left unset → the broker fails closed (no token verifies). Production omits
        # the demo stack and passes its campus IdP values here.
        broker_env = {
            "AGATE_AUTHENTICATED_ROLE_ARN": authenticated_role.role_arn,
            "AGATE_SESSION_DURATION_SECONDS": "900",
        }
        for env_key, ctx_key in (
            ("AGATE_OIDC_ISSUER", "oidc_issuer"),
            ("AGATE_OIDC_JWKS_URL", "oidc_jwks_url"),
            ("AGATE_OIDC_AUDIENCE", "oidc_audience"),
            # Optional source-IP fence on the broker. Comma-separated CIDRs/IPs via
            # `-c allow_ip=1.2.3.4` (or 1.2.3.4/32, a.b.c.0/24). Empty = open. The
            # HTTP API has no resource policy, so the broker enforces this itself.
            ("AGATE_IP_ALLOWLIST", "allow_ip"),
        ):
            value = self.node.try_get_context(ctx_key)
            if value:
                broker_env[env_key] = value

        # --- Broker Lambda -------------------------------------------------
        # Bundles infra/ + agate/ + policy/ source AND PyJWT, so claims_to_tags and the
        # real token verifier (agate.jwt_verify) run in-Lambda (SEC-4).
        broker = lambda_.Function(
            self,
            "Broker",
            function_name=f"{HANDLE}-broker",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="infra.functions.broker.handler.handler",
            code=pip_bundled_code("agate", "infra", "policy"),
            timeout=cdk.Duration.seconds(10),
            memory_size=256,
            environment=broker_env,
            description="agate claims -> scoped-STS credential broker (per-request, zero idle)",
        )

        # --- Broker HTTP endpoint (API Gateway HTTP API) -------------------
        # The browser SPA POSTs {idp_token} here to exchange its IdP token for
        # scoped STS creds. No API-level auth: the broker authenticates the caller
        # from the JWT itself (verified RS256/JWKS, SEC-4) — there is no AWS
        # principal to IAM-auth, and the endpoint vends nothing without a valid
        # token. An HTTP API is per-request (NO CLOCKS) — no idle fee, no ALB.
        #
        # NB: we deliberately do NOT use a Lambda Function URL here. Public
        # (AuthType NONE) Function URLs are blocked by an account/org guardrail
        # (Lambda Block Public Access) in this environment — they return a 403
        # "Forbidden" at the edge before the handler runs. An HTTP API integration
        # invokes the broker via IAM (the service principal), so it is unaffected.
        http_api = apigwv2.HttpApi(
            self,
            "BrokerApi",
            api_name=f"{HANDLE}-broker",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],  # demo: any origin; pin to the SiteUrl for prod
                allow_methods=[apigwv2.CorsHttpMethod.POST],
                allow_headers=["content-type"],
            ),
        )
        http_api.add_routes(
            path="/",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration("BrokerIntegration", broker),
        )
        # HttpApi.url has a trailing slash; the SPA POSTs to this exact URL.
        broker_endpoint = http_api.url or ""

        # The broker is allowed to assume the authenticated role AND tag the session.
        broker.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=[authenticated_role.role_arn],
            )
        )
        # Reflect that on the role's trust policy.
        authenticated_role.assume_role_policy.add_statements(  # type: ignore[union-attr]
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[broker.grant_principal],
                actions=["sts:AssumeRole", "sts:TagSession"],
            )
        )

        # --- Cognito Identity Pool (federated, no User Pool) ---------------
        # SAML/OIDC providers are deploy-time config (campus IdP). Supply via
        # `-c saml_provider_arn=...` / `-c oidc_provider_url=...`; both default to
        # PLACEHOLDER and the pool is created with whatever is provided.
        saml_arn = self.node.try_get_context("saml_provider_arn")
        # OIDC providers are wired in Phase 4 alongside the real campus IdP.

        auth_providers = idpool.IdentityPoolAuthenticationProviders(
            saml_providers=([idpool.IdentityPoolProviderUrl.saml(saml_arn)] if saml_arn else None),
            open_id_connect_providers=None,  # wired in Phase 4 with a real OIDC provider
        )

        pool = idpool.IdentityPool(
            self,
            "IdentityPool",
            identity_pool_name=f"{HANDLE}",
            allow_unauthenticated_identities=False,
            authenticated_role=authenticated_role,
            authentication_providers=auth_providers if saml_arn else None,
        )

        # --- Outputs -------------------------------------------------------
        cdk.CfnOutput(self, "IdentityPoolId", value=pool.identity_pool_id)
        cdk.CfnOutput(self, "AuthenticatedRoleArn", value=authenticated_role.role_arn)
        cdk.CfnOutput(self, "BrokerFunctionName", value=broker.function_name)
        cdk.CfnOutput(self, "BrokerUrl", value=broker_endpoint)
        cdk.CfnOutput(
            self,
            "FederationStatus",
            value=("saml-configured" if saml_arn else f"{PLACEHOLDER}-no-idp-wired"),
        )

        self.authenticated_role = authenticated_role
        self.model_policy_doc = model_policy_doc
        self.data_policy_doc = data_policy_doc


# Convenience for the policy-simulation proof: expose the tag keys used.
TIER_TAG_KEY = tag_key("tier")
TENANT_TAG_KEY = tag_key("tenant")
