"""Phase 12 — graphical authoring endpoint (#117, vision §8.5).

Serves the visual builder / template gallery: a thin Lambda behind an IAM-authed Function URL
that returns the bounded authoring menu (`authoring_options` — only tiers ≤ the author's and
scope nodes the author contains) and disposes a builder-assembled spec through the SAME
compiler an LLM draft uses (`author_from_options` → `dispose_draft`). Graphical authoring is
the SAFEST surface, not a dumbed-down one: unsafe is unrepresentable in the menu AND clamped
by the compiler even if the UI is bypassed.

Security: this endpoint makes NO model call and performs NO write — it only reads the bounded
menu and runs the pure clamp. So it needs neither a Bedrock grant (unlike #118b drafting) nor
an S3 write role (unlike #118 deploy); deploy-on-confirm is the separate #118 deploy endpoint.
The only authority is the verified token (tier/scope/courses); the menu + the dispose both
derive from it, never from the body.

NO CLOCKS: a Function URL on a per-request Lambda — no ALB, no always-on box. AWS_IAM-authed
(the SPA signs with broker-vended scoped creds). Default-fleet stack.
"""

from __future__ import annotations

import aws_cdk as cdk
from agate.names import HANDLE
from aws_cdk import (
    Stack,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from constructs import Construct
from infra.assets import oidc_env_from_context, pip_bundled_code


class AuthoringStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        fn = lambda_.Function(
            self,
            "Authoring",
            function_name=f"{HANDLE}-authoring",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="infra.functions.authoring.handler.handler",
            code=pip_bundled_code("agate", "infra"),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            environment={
                "AGATE_REGION": self.region,
                # The verified-token coords (issuer + JWKS + audience).
                **oidc_env_from_context(self.node),
            },
            description="agate graphical authoring - bounded menu + compiler clamp (#117)",
        )
        # NO Bedrock / S3 / STS grant: this endpoint only reads the bounded menu and runs the
        # pure clamp (the author's authority is the verified token). The Lambda's default
        # execution role (logs only) suffices — least privilege by construction.

        # Function URL, IAM-authed (the SPA signs with the broker-vended scoped creds).
        url = fn.add_function_url(auth_type=lambda_.FunctionUrlAuthType.AWS_IAM)

        cdk.CfnOutput(self, "AuthoringUrl", value=url.url)
        cdk.CfnOutput(self, "AuthoringFunction", value=fn.function_name)

        self.function = fn
