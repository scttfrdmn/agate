"""Corpus endpoint (#191) — upload + browse a user's own in-scope documents.

An authenticated user uploads documents into, and lists, their own tenant+scope subtree
of the docs bucket. The S3 ObjectCreated trigger (in agate-data) then embeds an upload
into the tenant's vector index, so a newly uploaded doc becomes retrievable.

Security (the #84/#130 pattern): the endpoint derives tenant/scope from the VERIFIED
token and reads/writes/lists through a tenant-fenced role it ASSUMES with the verified
`agate:` tags — so the acting principal carries the tag `corpus_rw_policy`'s
`${aws:PrincipalTag/...}` fence binds. The broadly-vended browser role stays read-only
(its boundary denies PutObject); this write/list authority lives only on the assumed role.

NO CLOCKS: a Function URL on a per-request Lambda (S3 PUT/LIST is per-request). AWS_IAM-
authed (the SPA signs with broker-vended scoped creds). Default-fleet stack.
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
from infra.assets import function_url_cors, oidc_env_from_context, pip_bundled_code
from policy.generate import corpus_rw_policy


class CorpusStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = self.region
        account = self.account
        docs_bucket = f"{DOCS_BUCKET_PREFIX}-{account}-{region}"

        fn = lambda_.Function(
            self,
            "Corpus",
            function_name=f"{HANDLE}-corpus",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="infra.functions.corpus.handler.handler",
            code=pip_bundled_code("agate", "infra", "policy"),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            environment={
                "AGATE_REGION": region,
                "AGATE_DOCS_BUCKET": docs_bucket,
                **oidc_env_from_context(self.node),
            },
            description="agate corpus - upload + list a user's in-scope documents (#191)",
        )

        # --- Tenant-fenced corpus role (the ABAC boundary) ----------------
        # The role the Lambda ASSUMES per-request with the session's `agate:` tags. The
        # read/write/list fence (`corpus_rw_policy`) lives HERE — confined to
        # `{tenant}/{scope}/*` — so it binds the tag-bearing assumed credential, not the
        # un-tagged Lambda role. Trusted by the Lambda role for AssumeRole + TagSession.
        corpus_role = iam.Role(
            self,
            "CorpusRole",
            assumed_by=fn.grant_principal,
            description="agate: tenant-fenced corpus read/write/list role; assumed by the Lambda",
            max_session_duration=cdk.Duration.hours(1),
        )
        corpus_role.assume_role_policy.add_statements(  # type: ignore[union-attr]
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[fn.grant_principal],
                actions=["sts:TagSession"],
            )
        )
        corpus_role.attach_inline_policy(
            iam.Policy(
                self,
                "CorpusRW",
                document=iam.PolicyDocument.from_json(corpus_rw_policy(bucket=docs_bucket)),
            )
        )
        # The Lambda may assume (and tag) ONLY that role — its sole S3 authority.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=[corpus_role.role_arn],
            )
        )
        fn.add_environment("AGATE_CORPUS_ROLE_ARN", corpus_role.role_arn)

        # Function URL, IAM-authed (the SPA signs with the broker-vended scoped creds).
        url = fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            cors=function_url_cors(self.node),
        )

        # The browser's authenticated role must be allowed to INVOKE the IAM-authed
        # Function URL. As of Oct 2025 that needs BOTH lambda:InvokeFunctionUrl AND
        # lambda:InvokeFunction (#190); grant both as resource permissions to the auth
        # role (constructed by name — same account, no cross-stack import). identity.py
        # carries the matching identity-side grant (a boundaried role needs both).
        auth_role_arn = f"arn:aws:iam::{account}:role/{HANDLE}-authenticated"
        fn.add_permission(
            "InvokeUrlFromAuthRole",
            principal=iam.ArnPrincipal(auth_role_arn),
            action="lambda:InvokeFunctionUrl",
            function_url_auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
        )
        fn.add_permission(
            "InvokeFunctionFromAuthRole",
            principal=iam.ArnPrincipal(auth_role_arn),
            action="lambda:InvokeFunction",
            invoked_via_function_url=True,
        )

        cdk.CfnOutput(self, "CorpusUrl", value=url.url)
        cdk.CfnOutput(self, "CorpusFunction", value=fn.function_name)
        cdk.CfnOutput(self, "CorpusRoleArn", value=corpus_role.role_arn)

        self.function = fn
        self.corpus_role = corpus_role
