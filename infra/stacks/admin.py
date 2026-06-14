"""Phase 9 Track 1 — governed-access console API (#63, slice 2).

A small, independently-deployable stack: the admin Lambda behind its own API
Gateway HTTP API. The Lambda verifies the campus token and requires the verified
`agate:role == admin` before returning spend analytics — admin is gated at the
credential boundary (the differentiator vs NebulaONE/Quick), not in the SPA.

The SPA itself is the SAME `agate-web` build: the admin view only renders for an
admin session, and the real gate is the 403 this API returns for everyone else.

NO CLOCKS: per-request Lambda + HTTP API + a read grant on the existing spend
table. Nothing idle. The spend table is owned by `agate-audit`; we reference it by
its deterministic name + ARN to avoid a hard cross-stack dependency (the admin API
is useful even before audit is deployed — it just returns empty analytics).
"""

from __future__ import annotations

import aws_cdk as cdk
from agate.names import HANDLE
from aws_cdk import Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct
from infra.assets import pip_bundled_code


class AdminStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = self.region
        account = self.account
        spend_table_name = f"{HANDLE}-spend"
        spend_table_arn = f"arn:aws:dynamodb:{region}:{account}:table/{spend_table_name}"

        # OIDC verification config — same context keys as the broker so one deploy
        # invocation configures both (campus IdP or the demo pool).
        admin_env = {"AGATE_SPEND_TABLE": spend_table_name}
        for env_key, ctx_key in (
            ("AGATE_OIDC_ISSUER", "oidc_issuer"),
            ("AGATE_OIDC_JWKS_URL", "oidc_jwks_url"),
            ("AGATE_OIDC_AUDIENCE", "oidc_audience"),
        ):
            value = self.node.try_get_context(ctx_key)
            if value:
                admin_env[env_key] = value

        admin_fn = lambda_.Function(
            self,
            "AdminApi",
            function_name=f"{HANDLE}-admin",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="infra.functions.admin.handler.handler",
            code=pip_bundled_code("agate", "infra"),
            timeout=cdk.Duration.seconds(10),
            memory_size=256,
            environment=admin_env,
            description="agate governed-access console API - admin-gated spend analytics",
        )

        # Read-only on the spend table (scan + get). The admin path never writes.
        admin_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem", "dynamodb:Scan", "dynamodb:Query"],
                resources=[spend_table_arn],
            )
        )

        # Public HTTP API (no API-level auth — the Lambda authenticates from the JWT
        # and requires role==admin). Per-request, NO CLOCKS. (Function URLs are
        # blocked in this account; HTTP API is the working browser front door.)
        http_api = apigwv2.HttpApi(
            self,
            "AdminHttpApi",
            api_name=f"{HANDLE}-admin",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],  # demo: any origin; pin to the SiteUrl for prod
                allow_methods=[apigwv2.CorsHttpMethod.POST],
                allow_headers=["content-type"],
            ),
        )
        http_api.add_routes(
            path="/",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration("AdminIntegration", admin_fn),
        )

        cdk.CfnOutput(self, "AdminUrl", value=http_api.url or "")
        cdk.CfnOutput(self, "AdminFunctionName", value=admin_fn.function_name)
