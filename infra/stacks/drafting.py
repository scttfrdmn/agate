"""Phase 12 — natural-language drafting endpoint (#118b, vision §8.5).

The live surface for "the LLM proposes, the compiler disposes": a thin Lambda behind an
IAM-authed Function URL that asks the author's OWN entitled model to draft an agent spec, then
clamps it to the author's verified authority via `agate.drafting.dispose_draft` and returns the
bounded plan to confirm. Nothing compiles to a live agent — draft → clamp → render only.

Security: the model output has ZERO authority (the disposer clamps to the verified author
tags), so the model call needs no per-tenant data fence. The Lambda invokes Bedrock under its
own role, scoped to the entitled-model SUPERSET; the per-session tier is enforced in the
handler by drafting with `models_for_tier(verified_tier)` — the agent-runtime discipline (the
IAM bound is the outer universe, not the per-user scope).

NO CLOCKS: a Function URL on a per-request Lambda — no ALB, no always-on container. The
Function URL is AWS_IAM-authed (the SPA signs with the broker-vended scoped creds), not public.
Bedrock is per-request / $0-idle, so this is a default-fleet stack (no opt-in gate).
"""

from __future__ import annotations

import aws_cdk as cdk
from agate.entitlements import model_arns_for_tier
from agate.names import HANDLE
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
from infra.assets import function_url_cors, oidc_env_from_context, pip_bundled_code


class DraftingStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = self.region
        account = self.account

        fn = lambda_.Function(
            self,
            "Drafting",
            function_name=f"{HANDLE}-drafting",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="infra.functions.drafting.handler.handler",
            code=pip_bundled_code("agate", "infra"),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            environment={
                "AGATE_REGION": region,
                # The verified-token coords (issuer + JWKS + audience) the broker/Runtime trust.
                **oidc_env_from_context(self.node),
                "AGATE_DRAFT_MAX_TOKENS": "1024",
            },
            description="agate drafting endpoint - LLM drafts, the compiler disposes (#118b)",
        )

        # Bedrock invoke bounded to agate's entitled models (the frontier superset, cumulative).
        # Per-SESSION tier is enforced in the handler against the verified token — this IAM
        # bound is the outer universe, never the per-user scope. The drafted spec carries no
        # authority (dispose_draft clamps to the verified author tags), so no data perms here.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="BedrockDraftInvoke",
                effect=iam.Effect.ALLOW,
                actions=["bedrock:Converse", "bedrock:InvokeModel"],
                resources=model_arns_for_tier("frontier", region=region, account=account),
            )
        )

        # Function URL, IAM-authed (the SPA signs with the broker-vended scoped creds). No
        # public access, no ALB, no clock.
        url = fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            cors=function_url_cors(self.node),
        )

        cdk.CfnOutput(self, "DraftingUrl", value=url.url)
        cdk.CfnOutput(self, "DraftingFunction", value=fn.function_name)

        self.function = fn
