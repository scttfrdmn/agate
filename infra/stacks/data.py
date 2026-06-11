"""Phase 3 — the data plane (design §4, §12 Phase 3).

Per-tenant document storage + vector retrieval, all storage-priced (NO CLOCKS):
  * one `agg-docs-*` S3 bucket, partitioned by tenant prefix `s3://.../{tenant}/...`
  * one S3 Vectors **vector bucket** + one **index per tenant**, each tagged with
    `agg:tenant` so the Phase 1 ABAC data-scope policy isolates reads.
  * a **per-tenant KMS CMK** on each vector index (security memo §6: per-index CMK).

S3 Vectors has no L2 construct yet, so we use the L1 `Cfn*` constructs from
`aws_cdk.aws_s3vectors` (CLAUDE.md: use L1 where no L2 exists). Tenants are
deploy-time config (the university org chart IS the tenancy model, design §7):
supply `-c tenants=chem,psych,kempner`; defaults to a single demo tenant.

Embeddings are `amazon.titan-embed-text-v2:0` (1024-dim, cosine) — the ingest
Lambda and the web query path must agree on this; it is the one knob that, if
changed, requires re-embedding every index.
"""

from __future__ import annotations

import aws_cdk as cdk
from agg.names import DOCS_BUCKET_PREFIX, HANDLE, tag_key
from aws_cdk import (
    Stack,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_kms as kms,
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
    aws_s3vectors as s3vectors,
)
from constructs import Construct

# Embedding model contract — shared with ingest/ and web/src/rag. Changing the
# dimension or model means re-embedding; keep it in one place.
EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBED_DIMENSION = 1024
DISTANCE_METRIC = "cosine"
DATA_TYPE = "float32"

DEFAULT_TENANTS = ["demo"]


def _parse_tenants(raw: object) -> list[str]:
    if not raw:
        return list(DEFAULT_TENANTS)
    items = [str(t) for t in raw] if isinstance(raw, list) else str(raw).split(",")
    return [t.strip() for t in items if t.strip()]


class DataStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        tenants = _parse_tenants(self.node.try_get_context("tenants"))

        # --- Documents bucket (per-tenant prefix) -------------------------
        # Bucket-name suffix keeps it globally unique without a clock; objects
        # land under {tenant}/... and the ingest Lambda derives tenant from that.
        docs_bucket = s3.Bucket(
            self,
            "DocsBucket",
            bucket_name=f"{DOCS_BUCKET_PREFIX}-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,  # education records: never auto-delete
            versioned=True,
        )

        # --- S3 Vectors: one vector bucket, one index per tenant ----------
        vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=f"{HANDLE}-vectors-{self.account}-{self.region}",
        )

        self.tenant_indexes: dict[str, s3vectors.CfnIndex] = {}
        self.tenant_keys: dict[str, kms.Key] = {}

        for tenant in tenants:
            # Per-tenant CMK (security memo §6, §9). Rotated; retained with records.
            key = kms.Key(
                self,
                f"Cmk{_pascal(tenant)}",
                alias=f"alias/{HANDLE}-{tenant}",
                enable_key_rotation=True,
                description=f"agg per-tenant CMK for {tenant} vector index",
                removal_policy=cdk.RemovalPolicy.RETAIN,
            )

            index = s3vectors.CfnIndex(
                self,
                f"Index{_pascal(tenant)}",
                vector_bucket_name=vector_bucket.vector_bucket_name,
                index_name=f"{HANDLE}-{tenant}",
                data_type=DATA_TYPE,
                dimension=EMBED_DIMENSION,
                distance_metric=DISTANCE_METRIC,
                encryption_configuration=s3vectors.CfnIndex.EncryptionConfigurationProperty(
                    sse_type="aws:kms",
                    kms_key_arn=key.key_arn,
                ),
                # The tenant tag is the ABAC isolation primitive (Phase 1 §13.3):
                # the data-scope policy permits QueryVectors only where the index's
                # agg:tenant tag matches the session's agg:tenant principal tag.
                tags=[cdk.CfnTag(key=tag_key("tenant"), value=tenant)],
            )
            index.add_dependency(vector_bucket)

            self.tenant_indexes[tenant] = index
            self.tenant_keys[tenant] = key

        # --- Embed-on-upload ingest Lambda --------------------------------
        # S3 ObjectCreated -> embed -> PutVectors. Per-request, scales to zero.
        ingest_fn = lambda_.Function(
            self,
            "Ingest",
            function_name=f"{HANDLE}-ingest",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="ingest.handler.handler",
            code=lambda_.Code.from_asset(
                ".",
                exclude=[
                    "web",
                    "cli",
                    "docs",
                    "tests",
                    ".git",
                    "**/__pycache__",
                    "infra/cdk.out",
                    ".venv",
                    "node_modules",
                ],
            ),
            timeout=cdk.Duration.minutes(2),
            memory_size=512,
            environment={
                "AGG_EMBED_MODEL_ID": EMBED_MODEL_ID,
                "AGG_EMBED_DIMENSION": str(EMBED_DIMENSION),
                "AGG_VECTOR_BUCKET": vector_bucket.vector_bucket_name,
            },
            description="agg: embed-on-upload ingest (S3 -> Bedrock embeddings -> S3 Vectors)",
        )

        docs_bucket.grant_read(ingest_fn)
        ingest_fn.add_event_source(
            lambda_events.S3EventSource(docs_bucket, events=[s3.EventType.OBJECT_CREATED])
        )
        # Embeddings: invoke only the embedding model (least privilege).
        ingest_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{EMBED_MODEL_ID}"],
            )
        )
        # Write vectors into every tenant index; decrypt via the per-tenant CMKs.
        ingest_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3vectors:PutVectors"],
                resources=["*"],
            )
        )
        for key in self.tenant_keys.values():
            key.grant_encrypt_decrypt(ingest_fn)

        # --- Outputs -------------------------------------------------------
        cdk.CfnOutput(self, "DocsBucketName", value=docs_bucket.bucket_name)
        cdk.CfnOutput(self, "VectorBucketName", value=vector_bucket.vector_bucket_name)
        cdk.CfnOutput(self, "Tenants", value=",".join(tenants))
        cdk.CfnOutput(self, "EmbedModelId", value=EMBED_MODEL_ID)

        self.docs_bucket = docs_bucket
        self.vector_bucket = vector_bucket


def _pascal(s: str) -> str:
    """Tenant id -> a valid PascalCase CDK construct id fragment."""
    return "".join(part.capitalize() for part in s.replace("-", " ").replace("_", " ").split())
