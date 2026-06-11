"""Embed-on-upload ingest Lambda (design §4, §12 Phase 3).

Triggered by an S3 ObjectCreated event on the `agg-docs` bucket. For each object:
  1. derive the tenant from the key prefix (fail closed if absent),
  2. read + chunk the document (pure logic in agg.rag),
  3. embed each chunk with Bedrock Titan embeddings,
  4. PutVectors into that tenant's S3 Vectors index.

This is the alternative to a Bedrock Knowledge Base (design §3): a small,
per-request function with no standing cost. Tenant isolation is structural — a
chunk is written ONLY to the index named for the key's tenant prefix, so a
misfiled object cannot leak into another tenant's index.

No clock: S3 event -> Lambda -> Bedrock + S3 Vectors, all per-request/per-byte.
"""

from __future__ import annotations

import json
import os
from urllib.parse import unquote_plus

import boto3
from agg.rag import build_chunk_records, index_name_for_tenant, tenant_from_s3_key

# Embedding contract — MUST match infra/stacks/data.py (EMBED_MODEL_ID/DIMENSION).
EMBED_MODEL_ID = os.environ.get("AGG_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIMENSION = int(os.environ.get("AGG_EMBED_DIMENSION", "1024"))
VECTOR_BUCKET = os.environ.get("AGG_VECTOR_BUCKET", "")
# Max bytes we'll pull into memory for a single object (keeps the Lambda bounded).
MAX_OBJECT_BYTES = int(os.environ.get("AGG_MAX_OBJECT_BYTES", str(5 * 1024 * 1024)))

_s3 = boto3.client("s3")
_bedrock = boto3.client("bedrock-runtime")
_vectors = boto3.client("s3vectors")


def embed(text: str) -> list[float]:
    """Embed one chunk with Titan; returns a 1024-dim normalized vector."""
    body = json.dumps({"inputText": text, "dimensions": EMBED_DIMENSION, "normalize": True})
    resp = _bedrock.invoke_model(modelId=EMBED_MODEL_ID, body=body)
    payload = json.loads(resp["body"].read())
    return payload["embedding"]


def _read_text(bucket: str, key: str) -> str:
    """Read a text-like object. Phase 3 handles UTF-8 text; binary parsers (PDF,
    docx) are a later enhancement — non-decodable objects are skipped, not guessed."""
    obj = _s3.get_object(Bucket=bucket, Key=key)
    size = obj.get("ContentLength", 0)
    if size > MAX_OBJECT_BYTES:
        raise ValueError(f"object {key} exceeds {MAX_OBJECT_BYTES} bytes")
    raw = obj["Body"].read()
    return raw.decode("utf-8")


def ingest_object(bucket: str, key: str) -> int:
    """Ingest one S3 object into its tenant's vector index. Returns chunk count."""
    tenant = tenant_from_s3_key(key)  # raises TenantKeyError -> caller skips
    index_name = index_name_for_tenant(tenant)

    text = _read_text(bucket, key)
    records = build_chunk_records(key, text)
    if not records:
        return 0

    vectors = [
        {
            "key": r.key,
            "data": {"float32": embed(r.text)},
            "metadata": r.metadata,
        }
        for r in records
    ]
    _vectors.put_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=index_name,
        vectors=vectors,
    )
    return len(vectors)


def handler(event: dict, context: object) -> dict:
    """S3 event entry point. Processes each record; one bad object never aborts
    the batch — it is logged and skipped (fail closed for that object only)."""
    results = []
    for rec in event.get("Records", []):
        s3rec = rec.get("s3", {})
        bucket = s3rec.get("bucket", {}).get("name", "")
        key = unquote_plus(s3rec.get("object", {}).get("key", ""))
        try:
            n = ingest_object(bucket, key)
            results.append({"key": key, "chunks": n, "status": "ok"})
        except Exception as exc:  # noqa: BLE001 — isolate per-object failures
            results.append({"key": key, "status": "skipped", "reason": str(exc)})
    return {"processed": results}
