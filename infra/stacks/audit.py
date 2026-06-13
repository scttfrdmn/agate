"""Phase 5 — governance/audit + authoritative spend (design §7.2, §12 Phase 5).

Stands up the authoritative-spend path the soft cap depends on (all storage- or
per-request-priced — NO CLOCKS):
  * a restricted **audit log bucket** for Bedrock model-invocation logs + CloudTrail.
  * **Bedrock invocation logging** enabled to that bucket, via an AwsCustomResource
    (account-level config — `PutModelInvocationLoggingConfiguration` has no CFN
    resource type, so a custom resource is the supported path).
  * the **`spend` DynamoDB table** (on-demand, PK `{tenant}#{user}#{period}` + the
    `{tenant}#{period}` rollup, design §13.6).
  * the **spend Lambda** (`meter.handler`) triggered on new log objects — it
    recomputes dollars from the logged token counts × Price List rates and upserts
    the table. The broker reads this at credential refresh for the soft cap.

Cost-allocation tags are applied at the stack level so per-tenant Bedrock spend is
attributable in Cost Explorer alongside the log-derived figure.
"""

from __future__ import annotations

import aws_cdk as cdk
from agg.names import HANDLE
from aws_cdk import (
    Stack,
)
from aws_cdk import (
    aws_dynamodb as ddb,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_lambda_event_sources as lambda_events,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    custom_resources as cr,
)
from constructs import Construct
from infra.assets import LAMBDA_ASSET_EXCLUDES


class AuditStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Audit log bucket (restricted; storage-priced) ----------------
        log_bucket = s3.Bucket(
            self,
            "AuditLogs",
            bucket_name=f"{HANDLE}-audit-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,  # audit trail: never auto-delete
            versioned=True,
        )
        # Bedrock's logging service principal must be able to write log objects.
        log_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowBedrockInvocationLogDelivery",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("bedrock.amazonaws.com")],
                actions=["s3:PutObject"],
                resources=[log_bucket.arn_for_objects("bedrock-invocation-logs/*")],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )

        # --- spend table (on-demand; §13.6) -------------------------------
        spend_table = ddb.Table(
            self,
            "Spend",
            table_name=f"{HANDLE}-spend",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.RETAIN,  # chargeable record of spend
            point_in_time_recovery=True,
        )

        # --- spend Lambda (recompute authoritative spend from logs) -------
        spend_fn = lambda_.Function(
            self,
            "SpendMeter",
            function_name=f"{HANDLE}-spend-meter",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="meter.handler.handler",
            code=lambda_.Code.from_asset(".", exclude=LAMBDA_ASSET_EXCLUDES),
            timeout=cdk.Duration.minutes(2),
            memory_size=256,
            environment={"AGG_SPEND_TABLE": spend_table.table_name},
            description="agg: authoritative spend — invocation logs -> spend table",
        )
        log_bucket.grant_read(spend_fn)
        spend_table.grant_read_write_data(spend_fn)
        spend_fn.add_event_source(
            lambda_events.S3EventSource(
                log_bucket,
                events=[s3.EventType.OBJECT_CREATED],
                filters=[s3.NotificationKeyFilter(prefix="bedrock-invocation-logs/")],
            )
        )

        # --- Enable Bedrock invocation logging (account-level) ------------
        # No CFN resource type exists for this account-level config, so use an
        # AwsCustomResource that calls PutModelInvocationLoggingConfiguration on
        # create/update and DeleteModelInvocationLoggingConfiguration on delete.
        logging_cr = cr.AwsCustomResource(
            self,
            "EnableInvocationLogging",
            on_create=cr.AwsSdkCall(
                service="Bedrock",
                action="putModelInvocationLoggingConfiguration",
                parameters={
                    "loggingConfig": {
                        "s3Config": {
                            "bucketName": log_bucket.bucket_name,
                            "keyPrefix": "bedrock-invocation-logs/",
                        },
                        "textDataDeliveryEnabled": True,
                        "embeddingDataDeliveryEnabled": True,
                        "imageDataDeliveryEnabled": False,
                    }
                },
                physical_resource_id=cr.PhysicalResourceId.of(f"{HANDLE}-invocation-logging"),
            ),
            on_delete=cr.AwsSdkCall(
                service="Bedrock",
                action="deleteModelInvocationLoggingConfiguration",
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE
            ),
        )
        logging_cr.node.add_dependency(log_bucket)

        # --- Cost-allocation tags (per-tenant chargeback attribution) -----
        cdk.Tags.of(self).add("agg:component", "audit")

        # --- Outputs -------------------------------------------------------
        cdk.CfnOutput(self, "AuditBucketName", value=log_bucket.bucket_name)
        cdk.CfnOutput(self, "SpendTableName", value=spend_table.table_name)
        cdk.CfnOutput(self, "SpendMeterFunction", value=spend_fn.function_name)

        self.log_bucket = log_bucket
        self.spend_table = spend_table
