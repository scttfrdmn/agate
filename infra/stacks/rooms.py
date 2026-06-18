"""Phase 12 — collaborative rooms endpoint (#116, vision §7).

The live transport for scope-bounded human+agent collaboration: a Lambda behind an IAM-authed
Function URL with op-dispatched open/join/leave/post/events. Transport is POLLING (the SPA polls
`events?since=<cursor>`), NOT WebSocket — API Gateway WebSocket bills per-connection-minute,
breaking NO CLOCKS. The pure room algebra (N-way scope intersection, attribution, budget
cascade) is `agate.rooms`; this stack is the AWS edge.

Security (the #130/#118 lesson): the handler RE-DERIVES the room's scope/tier server-side from
members on every mutation (never trusting a stored/body scope; a disjoint member is refused —
never widens), and reads/writes the room object through a tenant-fenced role it ASSUMES with the
verified `agate:` tags — so the principal that touches S3 carries the tags `room_rw_policy`
fences. The Lambda's own role only `sts:AssumeRole`s that role + reads/writes the spend/budget
DDB rows for the per-member budget gate + debit.

NO CLOCKS: a Function URL on a per-request Lambda + on-demand DDB + per-request S3 — no ALB, no
standing socket. AWS_IAM-authed (the SPA signs with broker-vended scoped creds). Default-fleet.
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
from policy.generate import room_rw_policy


class RoomsStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = self.region
        account = self.account
        docs_bucket = f"{DOCS_BUCKET_PREFIX}-{account}-{region}"
        spend_table = self.node.try_get_context("spend_table") or f"{HANDLE}-spend"
        budget_table = self.node.try_get_context("budget_table") or f"{HANDLE}-budget"

        fn = lambda_.Function(
            self,
            "Rooms",
            function_name=f"{HANDLE}-rooms",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="infra.functions.rooms.handler.handler",
            code=pip_bundled_code("agate", "infra", "cost", "policy", "meter"),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            environment={
                "AGATE_REGION": region,
                "AGATE_DOCS_BUCKET": docs_bucket,
                "AGATE_SPEND_TABLE": spend_table,
                "AGATE_BUDGET_TABLE": budget_table,
                # The verified-token coords (issuer + JWKS + audience).
                **oidc_env_from_context(self.node),
            },
            description="agate collaborative rooms - intersection-scoped, attributed (#116)",
        )

        # --- Tenant-fenced room read/write role (the ABAC boundary) -------
        # The role the Lambda ASSUMES per-request with the verified `agate:` tags. The
        # `_rooms/` Get+Put fence (`room_rw_policy`) lives HERE — so it binds the tag-bearing
        # assumed credential, not the un-tagged Lambda role (the #130/#118 discipline). Trusted
        # by the Lambda role for AssumeRole + TagSession.
        room_role = iam.Role(
            self,
            "RoomRwRole",
            assumed_by=fn.grant_principal,
            description="agate: tenant-fenced room read/write role; assumed by the rooms Lambda",
            max_session_duration=cdk.Duration.hours(1),
        )
        room_role.assume_role_policy.add_statements(  # type: ignore[union-attr]
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[fn.grant_principal],
                actions=["sts:TagSession"],
            )
        )
        room_role.attach_inline_policy(
            iam.Policy(
                self,
                "RoomRw",
                document=iam.PolicyDocument.from_json(room_rw_policy(bucket=docs_bucket)),
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=[room_role.role_arn],
            )
        )
        fn.add_environment("AGATE_ROOM_ROLE_ARN", room_role.role_arn)

        # The per-member budget gate reads spend + budget; the debit writes scope spend rows.
        # (The Lambda's own role does this — the spend/budget tables are tenant-keyed PKs the
        # handler builds from the VERIFIED identity, the same as the chokepoint.)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="RoomBudgetGateAndDebit",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[
                    f"arn:aws:dynamodb:{region}:{account}:table/{spend_table}",
                    f"arn:aws:dynamodb:{region}:{account}:table/{budget_table}",
                ],
            )
        )

        # Function URL, IAM-authed (the SPA signs with the broker-vended scoped creds).
        url = fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            cors=function_url_cors(self.node),
        )

        cdk.CfnOutput(self, "RoomsUrl", value=url.url)
        cdk.CfnOutput(self, "RoomsFunction", value=fn.function_name)
        cdk.CfnOutput(self, "RoomRwRoleArn", value=room_role.role_arn)

        self.function = fn
        self.room_role = room_role
