"""Phase 4 — LTI 1.3 tool provider stack (design §6, §12 Phase 4, §13.5).

One HTTP API + one Lambda routing the four LTI endpoints, with two DynamoDB
on-demand tables (no clock):
  * registrations — one row per registered LMS platform (issuer/client_id/JWKS/...)
  * state         — short-lived OIDC state+nonce, single-use, with a TTL attribute

The Lambda needs PyJWT+cryptography at runtime, which are not in the Lambda base
image, so the asset bundles `lti/requirements.txt` alongside the `agg/` + `lti/`
source. Bundling runs locally (uv/pip) so `cdk synth` needs no Docker; if local
bundling is unavailable CDK falls back to the official build image.

NO CLOCKS: HTTP API + Lambda are per-request; DynamoDB is on-demand (PAY_PER_REQUEST).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import aws_cdk as cdk
import jsii
from agg.names import HANDLE
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
from infra.assets import LAMBDA_ASSET_EXCLUDES

# Repo root (three levels up from infra/stacks/lti.py).
_ROOT = Path(__file__).resolve().parent.parent.parent

# Bundling command (Docker fallback): install runtime deps into the asset, then
# copy the pure agg/ package and the lti/ handlers next to them.
_BUNDLE_CMD = (
    "set -e; "
    "pip install -r lti/requirements.txt -t /asset-output >/dev/null; "
    "cp -r agg /asset-output/; "
    "cp -r lti /asset-output/"
)


@jsii.implements(cdk.ILocalBundling)
class _LocalPipBundler:
    """Bundle the LTI Lambda locally (no Docker) by pip-installing requirements
    and copying the agg/ + lti/ source into the asset output dir. Falls back to
    the Docker image if pip isn't available."""

    def try_bundle(self, output_dir: str, options) -> bool:  # noqa: ARG002
        if shutil.which("pip") is None and shutil.which("pip3") is None:
            return False
        pip = shutil.which("pip3") or shutil.which("pip")
        try:
            subprocess.run(
                [
                    pip,
                    "install",
                    "-r",
                    str(_ROOT / "lti" / "requirements.txt"),
                    "-t",
                    output_dir,
                    "--quiet",
                ],
                check=True,
            )
            for pkg in ("agg", "lti"):
                shutil.copytree(
                    _ROOT / pkg,
                    Path(output_dir) / pkg,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
        except (subprocess.CalledProcessError, OSError):
            return False
        return True


def _lti_code() -> lambda_.Code:
    return lambda_.Code.from_asset(
        ".",
        exclude=LAMBDA_ASSET_EXCLUDES,
        bundling=cdk.BundlingOptions(
            image=lambda_.Runtime.PYTHON_3_13.bundling_image,
            command=["bash", "-c", _BUNDLE_CMD],
            local=_LocalPipBundler(),
        ),
    )


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
                "AGG_LTI_REGISTRATIONS_TABLE": registrations.table_name,
                "AGG_LTI_STATE_TABLE": state.table_name,
                # TOOL_BASE_URL is set after the API exists (below).
            },
            description="agg: LTI 1.3 tool provider (login/launch/jwks/deeplink)",
        )
        registrations.grant_read_data(fn)
        state.grant_read_write_data(fn)

        http_api = apigwv2.HttpApi(
            self,
            "LtiApi",
            api_name=f"{HANDLE}-lti",
            description="agg LTI 1.3 endpoints",
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
        fn.add_environment("AGG_TOOL_BASE_URL", http_api.api_endpoint)

        # --- Outputs -------------------------------------------------------
        cdk.CfnOutput(self, "LtiApiEndpoint", value=http_api.api_endpoint)
        cdk.CfnOutput(self, "LoginUrl", value=f"{http_api.api_endpoint}/lti/login")
        cdk.CfnOutput(self, "LaunchUrl", value=f"{http_api.api_endpoint}/lti/launch")
        cdk.CfnOutput(self, "JwksUrl", value=f"{http_api.api_endpoint}/.well-known/jwks.json")
        cdk.CfnOutput(self, "RegistrationsTable", value=registrations.table_name)
