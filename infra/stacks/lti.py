"""Phase 4 — LTI 1.3 tool provider stack (design §6, §12 Phase 4, §13.5).

One HTTP API + one Lambda routing the four LTI endpoints, with two DynamoDB
on-demand tables (no clock):
  * registrations — one row per registered LMS platform (issuer/client_id/JWKS/...)
  * state         — short-lived OIDC state+nonce, single-use, with a TTL attribute

The Lambda needs PyJWT+cryptography at runtime, which are not in the Lambda base
image, so the asset bundles `lti/requirements.txt` alongside the `agate/` + `lti/`
source. Bundling runs locally (uv/pip) so `cdk synth` needs no Docker; if local
bundling is unavailable CDK falls back to the official build image.

NO CLOCKS: HTTP API + Lambda are per-request; DynamoDB is on-demand (PAY_PER_REQUEST).
"""

from __future__ import annotations

import aws_cdk as cdk
from agate.names import HANDLE
from aws_cdk import (
    Stack,
)
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
)
from aws_cdk import (
    aws_apigatewayv2_integrations as apigw_integrations,
)
from aws_cdk import (
    aws_dynamodb as ddb,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from constructs import Construct
from infra.assets import pip_bundled_code


def _lti_code() -> lambda_.Code:
    # Bundles PyJWT + the agate/ and lti/ source (shared bundler in infra.assets).
    return pip_bundled_code("agate", "lti")


class LtiStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- DynamoDB (on-demand, no clock) -------------------------------
        registrations = ddb.Table(
            self,
            "Registrations",
            table_name=f"{HANDLE}-lti-registrations",
            partition_key=ddb.Attribute(name="issuer", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="client_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
        )
        state = ddb.Table(
            self,
            "State",
            table_name=f"{HANDLE}-lti-state",
            partition_key=ddb.Attribute(name="state", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",  # auto-expire stale nonces
            removal_policy=cdk.RemovalPolicy.DESTROY,  # transient, safe to drop
        )

        # --- Lambda + HTTP API --------------------------------------------
        fn = lambda_.Function(
            self,
            "LtiFn",
            function_name=f"{HANDLE}-lti",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="lti.handler.handler",
            code=_lti_code(),
            timeout=cdk.Duration.seconds(15),
            memory_size=256,
            environment={
                "AGATE_LTI_REGISTRATIONS_TABLE": registrations.table_name,
                "AGATE_LTI_STATE_TABLE": state.table_name,
                # TOOL_BASE_URL is set after the API exists (below).
            },
            description="agate: LTI 1.3 tool provider (login/launch/jwks/deeplink)",
        )
        registrations.grant_read_data(fn)
        state.grant_read_write_data(fn)

        http_api = apigwv2.HttpApi(
            self,
            "LtiApi",
            api_name=f"{HANDLE}-lti",
            description="agate LTI 1.3 endpoints",
        )
        integration = apigw_integrations.HttpLambdaIntegration("LtiIntegration", fn)

        routes = [
            ("/lti/login", [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST]),
            ("/lti/launch", [apigwv2.HttpMethod.POST]),
            ("/lti/deeplink", [apigwv2.HttpMethod.POST]),
            ("/.well-known/jwks.json", [apigwv2.HttpMethod.GET]),
        ]
        for path, methods in routes:
            http_api.add_routes(path=path, methods=methods, integration=integration)

        # The tool's own base URL (used to build redirect_uri + launch redirect).
        fn.add_environment("AGATE_TOOL_BASE_URL", http_api.api_endpoint)

        # --- Outputs -------------------------------------------------------
        cdk.CfnOutput(self, "LtiApiEndpoint", value=http_api.api_endpoint)
        cdk.CfnOutput(self, "LoginUrl", value=f"{http_api.api_endpoint}/lti/login")
        cdk.CfnOutput(self, "LaunchUrl", value=f"{http_api.api_endpoint}/lti/launch")
        cdk.CfnOutput(self, "JwksUrl", value=f"{http_api.api_endpoint}/.well-known/jwks.json")
        cdk.CfnOutput(self, "RegistrationsTable", value=registrations.table_name)
