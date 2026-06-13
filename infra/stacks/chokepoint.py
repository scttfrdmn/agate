"""Phase 6 — OPTIONAL Tier 1 choke point (design §2, §7.1, §12 Phase 6).

A thin Lambda behind a Function URL (response streaming) for institutions that
require EXACT pre-spend enforcement, centralized inspection, or non-Bedrock routing
— rather than the default Tier 0 soft cap. It reads authoritative spend from the
`spend` table, runs the exact pre-call budget gate, and on allow invokes Converse
**assuming the user's own scoped role** (same ABAC as Tier 0, plus enforcement).

This stack is built only when an institution opts into Tier 1; default deployments
never include it. NO CLOCKS: a Function URL on a per-request Lambda — no ALB, no
always-on container. The Function URL is AWS_IAM-authed (the SPA signs with the
broker-vended scoped creds), not public.

Cross-stack inputs (spend table name, authenticated role ARN) are passed via
context so this stack stays deployable on its own.
"""

from __future__ import annotations

import aws_cdk as cdk
from agg.names import HANDLE
from aws_cdk import (
    Stack,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from constructs import Construct
from infra.assets import LAMBDA_ASSET_EXCLUDES

PLACEHOLDER = "PLACEHOLDER"


class ChokepointStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Cross-stack wiring (supply at deploy: -c spend_table=... -c budget_table=...
        # -c auth_role_arn=...). Budget is read server-side, never from the request.
        spend_table = self.node.try_get_context("spend_table") or f"{HANDLE}-spend"
        budget_table = self.node.try_get_context("budget_table") or f"{HANDLE}-budget"
        auth_role_arn = self.node.try_get_context("auth_role_arn") or PLACEHOLDER

        fn = lambda_.Function(
            self,
            "Chokepoint",
            function_name=f"{HANDLE}-chokepoint",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="chokepoint.handler.handler",
            code=lambda_.Code.from_asset(".", exclude=LAMBDA_ASSET_EXCLUDES),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            environment={
                "AGG_SPEND_TABLE": spend_table,
                "AGG_BUDGET_TABLE": budget_table,
                "AGG_AUTHENTICATED_ROLE_ARN": auth_role_arn,
                "AGG_DEFAULT_MAX_TOKENS": "1024",
            },
            description="agg Tier 1 choke point — exact pre-call budget enforcement (optional)",
        )

        # Read authoritative spend + the server-side budget; assume the user's scoped role.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem"],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/{spend_table}",
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/{budget_table}",
                ],
            )
        )
        if auth_role_arn != PLACEHOLDER:
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["sts:AssumeRole", "sts:TagSession"],
                    resources=[auth_role_arn],
                )
            )

        # Function URL with response streaming, IAM-authed (the SPA signs with the
        # broker-vended scoped creds). No public access, no ALB, no clock.
        url = fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
        )

        cdk.CfnOutput(self, "ChokepointUrl", value=url.url)
        cdk.CfnOutput(self, "ChokepointFunction", value=fn.function_name)
        cdk.CfnOutput(
            self,
            "AuthRoleStatus",
            value="wired"
            if auth_role_arn != PLACEHOLDER
            else f"{PLACEHOLDER}-pass -c auth_role_arn",
        )
