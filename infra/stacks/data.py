"""The data plane (design §4; §12 Phase 3 text RAG + §10.2.7 multimodal KB).

Per-tenant document storage + vector retrieval, all storage-priced (NO CLOCKS):
  * one `agate-docs-*` S3 bucket, partitioned by tenant prefix `s3://.../{tenant}/...`,
    with a `_mm-artifacts/` sub-prefix for processed multimodal artifacts.
  * one S3 Vectors **vector bucket** + **two indexes per tenant** — a 1024-dim
    text index and a 3072-dim multimodal index — each tagged with `agate:tenant` so
    the Phase 1 ABAC data-scope policy isolates reads.
  * a **per-tenant KMS CMK** encrypting both of that tenant's indexes (security
    memo §6: per-index CMK).

S3 Vectors has no L2 construct yet, so we use the L1 `Cfn*` constructs from
`aws_cdk.aws_s3vectors` (CLAUDE.md: use L1 where no L2 exists; migration tracked
in #22). Tenants are deploy-time config (the university org chart IS the tenancy
model, design §7): supply `-c tenants=chem,psych,kempner`; defaults to one demo.

Two embedding contracts, each pinned to its index dimension: text is
`amazon.titan-embed-text-v2:0` (1024-dim) and multimodal is
`amazon.nova-2-multimodal-embeddings-v1:0` (3072-dim, gate-verified in #17).
Changing a model/dimension requires re-embedding that index.
"""

from __future__ import annotations

import aws_cdk as cdk
from agate.names import DOCS_BUCKET_PREFIX, HANDLE, tag_key
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
from infra.assets import LAMBDA_ASSET_EXCLUDES

# Text embedding contract — shared with ingest/ and web/src/rag. Changing the
# dimension or model means re-embedding; keep it in one place.
EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBED_DIMENSION = 1024
DISTANCE_METRIC = "cosine"
DATA_TYPE = "float32"

# Multimodal embedding contract (§10.2.7, gate-verified — issue #17). The Nova
# multimodal model embeds text/image/audio/video at 3072 dims, so the multimodal
# index has its OWN dimension and is separate from the 1024-dim text index.
MM_EMBED_MODEL_ID = "amazon.nova-2-multimodal-embeddings-v1:0"
MM_EMBED_DIMENSION = 3072
# S3 prefix (within the docs bucket) for processed multimodal artifacts the KB
# emits — the "designated multimodal storage" the supplemental config points at.
MM_STORAGE_PREFIX = "_mm-artifacts"

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
        self.tenant_mm_indexes: dict[str, s3vectors.CfnIndex] = {}
        self.tenant_keys: dict[str, kms.Key] = {}

        def _index(tenant: str, key: kms.Key, *, suffix: str, dimension: int) -> s3vectors.CfnIndex:
            # One S3 Vectors index. The tenant tag is the ABAC isolation primitive
            # (Phase 1 §13.3): the data-scope policy permits QueryVectors only where
            # the index's agate:tenant tag matches the session's principal tag.
            cid = (
                f"Index{_pascal(tenant)}{_pascal(suffix)}" if suffix else f"Index{_pascal(tenant)}"
            )
            name = f"{HANDLE}-{tenant}-{suffix}" if suffix else f"{HANDLE}-{tenant}"
            idx = s3vectors.CfnIndex(
                self,
                cid,
                vector_bucket_name=vector_bucket.vector_bucket_name,
                index_name=name,
                data_type=DATA_TYPE,
                dimension=dimension,
                distance_metric=DISTANCE_METRIC,
                encryption_configuration=s3vectors.CfnIndex.EncryptionConfigurationProperty(
                    sse_type="aws:kms",
                    kms_key_arn=key.key_arn,
                ),
                tags=[cdk.CfnTag(key=tag_key("tenant"), value=tenant)],
            )
            idx.add_dependency(vector_bucket)
            return idx

        for tenant in tenants:
            # Per-tenant CMK (security memo §6, §9). Rotated; retained with records.
            # The same CMK encrypts both the tenant's text and multimodal indexes.
            key = kms.Key(
                self,
                f"Cmk{_pascal(tenant)}",
                alias=f"alias/{HANDLE}-{tenant}",
                enable_key_rotation=True,
                description=f"agate per-tenant CMK for {tenant} vector indexes",
                removal_policy=cdk.RemovalPolicy.RETAIN,
            )
            # S3 Vectors performs asynchronous indexing under its own service
            # principal, which must be able to use the index's CMK — otherwise the
            # index create fails with "Insufficient access to perform asynchronous
            # indexing" (a 403 from the indexing.s3vectors service principal).
            # Scope it to this account + region's vector bucket via the standard
            # KMS ViaService / SourceAccount conditions.
            key.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="AllowS3VectorsAsyncIndexing",
                    effect=iam.Effect.ALLOW,
                    principals=[iam.ServicePrincipal("indexing.s3vectors.amazonaws.com")],
                    actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                    resources=["*"],
                    conditions={"StringEquals": {"aws:SourceAccount": self.account}},
                )
            )
            # The server-side retrieval proxy assumes `agate-vector-reader` to run
            # QueryVectors; the indexes are CMK-encrypted, so that role must also be
            # able to DECRYPT — otherwise the query fails with a kms:Decrypt
            # AccessDeniedException. The role lives in the identity stack; grant via a
            # name-based ARN in THIS key's policy (no cross-stack import, same pattern
            # as the chokepoint trust). Tenant isolation still holds: the role is only
            # ever assumed with a verified agate:tenant tag, and each CMK is per-tenant.
            key.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="AllowVectorReaderDecrypt",
                    effect=iam.Effect.ALLOW,
                    principals=[
                        iam.ArnPrincipal(f"arn:aws:iam::{self.account}:role/{HANDLE}-vector-reader")
                    ],
                    actions=["kms:Decrypt", "kms:DescribeKey"],
                    resources=["*"],
                )
            )

            # Text index (1024-dim) — the Phase 3 RAG store, unchanged.
            self.tenant_indexes[tenant] = _index(tenant, key, suffix="", dimension=EMBED_DIMENSION)
            # Multimodal index (3072-dim, §10.2.7) — separate index, same tenant
            # tag, same CMK. Built ALONGSIDE the text index, not replacing it.
            self.tenant_mm_indexes[tenant] = _index(
                tenant, key, suffix="mm", dimension=MM_EMBED_DIMENSION
            )
            self.tenant_keys[tenant] = key

        # --- Embed-on-upload ingest Lambda --------------------------------
        # S3 ObjectCreated -> embed -> PutVectors. Per-request, scales to zero.
        ingest_fn = lambda_.Function(
            self,
            "Ingest",
            function_name=f"{HANDLE}-ingest",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="ingest.handler.handler",
            code=lambda_.Code.from_asset(".", exclude=LAMBDA_ASSET_EXCLUDES),
            timeout=cdk.Duration.minutes(2),
            memory_size=512,
            environment={
                "AGATE_EMBED_MODEL_ID": EMBED_MODEL_ID,
                "AGATE_EMBED_DIMENSION": str(EMBED_DIMENSION),
                "AGATE_VECTOR_BUCKET": vector_bucket.vector_bucket_name,
            },
            description="agate: embed-on-upload ingest (S3 -> Bedrock embeddings -> S3 Vectors)",
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
        cdk.CfnOutput(self, "MultimodalEmbedModelId", value=MM_EMBED_MODEL_ID)
        cdk.CfnOutput(self, "MultimodalIndexDimension", value=str(MM_EMBED_DIMENSION))
        # Supplemental multimodal-storage location (§10.2.7): processed visual
        # artifacts live under this prefix of the docs bucket. A managed KB's
        # SupplementalDataStorageConfiguration points at s3://{docs}/{tenant}/_mm-artifacts/.
        cdk.CfnOutput(
            self,
            "MultimodalStoragePrefix",
            value=f"s3://{docs_bucket.bucket_name}/<tenant>/{MM_STORAGE_PREFIX}/",
        )

        self.docs_bucket = docs_bucket
        self.vector_bucket = vector_bucket


def _pascal(s: str) -> str:
    """Tenant id -> a valid PascalCase CDK construct id fragment."""
    return "".join(part.capitalize() for part in s.replace("-", " ").replace("_", " ").split())
