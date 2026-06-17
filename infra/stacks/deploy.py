"""Phase 12 — deploy-on-confirm endpoint (#118 last slice, vision §8.5).

The final step of natural-language authoring: when a user confirms a drafted+clamped agent
plan, this endpoint CREATES the agent by persisting its governed spec as a scope-tagged S3
object (`{tenant}/{scope}/_agents/{name}.json`). Per §0.1 agate governs (records the spec +
the bound); the runtime/agenkit re-instantiates + runs it. No standing credential is vended.

Security (the #130 lesson): the endpoint RE-CLAMPS the confirmed spec against the verified
token server-side (never trusting the echoed spec as authority), and WRITES through a
tenant-fenced role it ASSUMES with the verified `agate:` tags — so the writing principal
carries the tenant/scope tags `agent_write_policy`'s `${aws:PrincipalTag/...}` fence binds. The
Lambda's own role holds NO S3 write; it can only assume that role. Mirrors the #84 retrieval
proxy + the #130 memory tool.

NO CLOCKS: a Function URL on a per-request Lambda — no ALB, no always-on box. AWS_IAM-authed
(the SPA signs with broker-vended scoped creds). Default-fleet stack (S3 PUT is per-request).
"""

from __future__ import annotations

import aws_cdk as cdk
from agate.names import DOCS_BUCKET_PREFIX, HANDLE
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
from infra.assets import pip_bundled_code
from policy.generate import agent_write_policy


class DeployStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = self.region
        account = self.account
        docs_bucket = f"{DOCS_BUCKET_PREFIX}-{account}-{region}"

        oidc_discovery_url = self.node.try_get_context("cognito_discovery_url") or ""
        allowed_audience = self.node.try_get_context("cognito_audience") or ""

        fn = lambda_.Function(
            self,
            "Deploy",
            function_name=f"{HANDLE}-agent-deploy",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="infra.functions.deploy.handler.handler",
            code=pip_bundled_code("agate", "infra", "policy"),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            environment={
                "AGATE_REGION": region,
                "AGATE_DOCS_BUCKET": docs_bucket,
                "AGATE_OIDC_ISSUER": oidc_discovery_url,
                "AGATE_OIDC_AUDIENCE": allowed_audience,
            },
            description="agate deploy-on-confirm - re-clamp + persist a created agent (#118)",
        )

        # --- Tenant-fenced agent-write role (the ABAC boundary) -----------
        # The role the Lambda ASSUMES per-request with the session's `agate:` tags. The
        # write fence (`agent_write_policy`) lives HERE — PutObject only under
        # `{tenant}/{scope}/_agents/*` — so it binds the tag-bearing assumed credential, not
        # the un-tagged Lambda role. Trusted by the Lambda role for AssumeRole + TagSession.
        write_role = iam.Role(
            self,
            "AgentDeployRole",
            assumed_by=fn.grant_principal,
            description="agate: tenant-fenced created-agent write role; assumed by deploy Lambda",
            max_session_duration=cdk.Duration.hours(1),
        )
        write_role.assume_role_policy.add_statements(  # type: ignore[union-attr]
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[fn.grant_principal],
                actions=["sts:TagSession"],
            )
        )
        write_role.attach_inline_policy(
            iam.Policy(
                self,
                "AgentWrite",
                document=iam.PolicyDocument.from_json(agent_write_policy(bucket=docs_bucket)),
            )
        )
        # The Lambda may assume (and tag) ONLY that role — its sole write authority.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=[write_role.role_arn],
            )
        )
        fn.add_environment("AGATE_AGENT_DEPLOY_ROLE_ARN", write_role.role_arn)

        # Function URL, IAM-authed (the SPA signs with the broker-vended scoped creds).
        url = fn.add_function_url(auth_type=lambda_.FunctionUrlAuthType.AWS_IAM)

        cdk.CfnOutput(self, "DeployUrl", value=url.url)
        cdk.CfnOutput(self, "DeployFunction", value=fn.function_name)
        cdk.CfnOutput(self, "AgentDeployRoleArn", value=write_role.role_arn)

        self.function = fn
        self.write_role = write_role
