"""Broker-proxied vector retrieval (design §4, #84) — makes sub-tenant scope REAL.

Until #84, the browser signed `QueryVectors` directly with its vended creds and
supplied the scope `filter` itself — so a modified client could omit/alter the filter
and read the whole tenant index. Scope was advisory; only the tenant was IAM-enforced.

This server-side proxy closes that: it derives the scope filter from the VERIFIED IdP
token (never a request field), embeds the query, and signs `QueryVectors` while
**assuming the dedicated `agate-vector-reader` role** — the ONLY identity that can
query vectors. The browser-held `agate-authenticated` role no longer has any
`s3vectors` grant (policy.generate: the `QueryOwnTenantVectors` Allow moved to
`vector_query_policy`), so there is no path that bypasses this proxy.

Serves BOTH the text index (`agate-{tenant}`, Titan 1024-dim) and the multimodal
index (`agate-{tenant}-mm`, Nova 3072-dim) — selected by `index_kind` — with the
SAME scope filter injected for each (#94 extends #84 to close the multimodal bypass).

WHERE THE BOUNDARY LIVES (flagged because it's split):
  * TENANT — IAM. `agate-vector-reader` is fenced by
    `aws:ResourceTag/agate:tenant == ${aws:PrincipalTag/agate:tenant}`; a cross-tenant
    query is denied by the credential (preserves the CISO §6 promise for vectors).
  * SCOPE (sub-tenant) — CODE, here. It is NOT IAM-enforceable: the index is
    per-tenant and scope is row metadata IAM can't read. The proxy injects
    `scope_filter(retrieval_nodes(tags.scope, tags.courses))` built from the verified
    token. What makes it a real boundary is that the only caller able to run
    QueryVectors is this proxy, and it always injects the filter.

Per-request Lambda behind a Function URL (AWS_IAM auth — the SPA signs with the
broker-vended scoped creds). No clock. Fails closed: any verification/scoping error
returns NO results.
"""

from __future__ import annotations

import json
import logging
import os

import boto3
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.multimodal import nova_embed_request, parse_nova_embedding
from agate.rag import (
    index_name_for_tenant,
    mm_index_name_for_tenant,
    retrieval_nodes,
    scope_filter,
)
from agate.tags import ClaimsError, claims_to_tags, role_session_name

VECTOR_READER_ROLE_ARN = os.environ.get("AGATE_VECTOR_READER_ROLE_ARN", "")
VECTOR_BUCKET = os.environ.get("AGATE_VECTOR_BUCKET", "")
EMBED_MODEL_ID = os.environ.get("AGATE_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIMENSION = int(os.environ.get("AGATE_EMBED_DIMENSION", "1024"))
MM_EMBED_MODEL_ID = os.environ.get(
    "AGATE_MM_EMBED_MODEL_ID", "amazon.nova-2-multimodal-embeddings-v1:0"
)
DEFAULT_TOP_K = int(os.environ.get("AGATE_DEFAULT_TOP_K", "5"))
MAX_TOP_K = int(os.environ.get("AGATE_MAX_TOP_K", "20"))

_sts = boto3.client("sts")
_bedrock = boto3.client("bedrock-runtime")


class RetrievalError(Exception):
    """Return-no-results error -> a terse 4xx, never a partial/unscoped result set."""


def validate_idp_token(token: str) -> dict:
    """Verify the campus-IdP token (real RS256/JWKS via the shared verifier). Same
    verifier the broker uses; any failure raises RetrievalError (fail closed)."""
    cfg = config_from_env()
    try:
        return verify_token(token, **cfg)
    except TokenError as exc:
        raise RetrievalError(str(exc)) from exc


def embed_query(query: str) -> list[float]:
    """Embed a TEXT query with Titan — server-side, so the SPA needs no embed grant
    and the embed contract can't drift from ingest (same EMBED_MODEL_ID/DIMENSION)."""
    body = json.dumps({"inputText": query, "dimensions": EMBED_DIMENSION, "normalize": True})
    resp = _bedrock.invoke_model(modelId=EMBED_MODEL_ID, body=body)
    return json.loads(resp["body"].read())["embedding"]


def embed_mm_query(
    text: str | None, image_b64: str | None, image_format: str | None
) -> list[float]:
    """Embed a MULTIMODAL query with Nova (text OR image) for the `-mm` index (#94).
    Reuses the gate-verified agate.multimodal request/response contract so the proxy
    and ingest embed identically. GENERIC_QUERY purpose (this is a query, not a store)."""
    body = json.dumps(
        nova_embed_request(
            text=text, image_b64=image_b64, image_format=image_format, purpose="GENERIC_QUERY"
        )
    )
    resp = _bedrock.invoke_model(modelId=MM_EMBED_MODEL_ID, body=body)
    return parse_nova_embedding(json.loads(resp["body"].read()))


def assume_vector_reader(tags, subject: str):
    """Assume `agate-vector-reader` narrowed by the VERIFIED agate: session tags,
    returning a scoped s3vectors client. The tenant tag fences which index the
    credential can read; the proxy supplies the scope FILTER. RoleSessionName ties
    the query to the federated subject + tenant (same encoding as the broker, #79)."""
    resp = _sts.assume_role(
        RoleArn=VECTOR_READER_ROLE_ARN,
        RoleSessionName=role_session_name(tags.tenant, subject),
        Tags=tags.to_sts_tags(),
        TransitiveTagKeys=[t["Key"] for t in tags.to_sts_tags()],
        DurationSeconds=900,
    )
    c = resp["Credentials"]
    return boto3.client(
        "s3vectors",
        aws_access_key_id=c["AccessKeyId"],
        aws_secret_access_key=c["SecretAccessKey"],
        aws_session_token=c["SessionToken"],
    )


def process(req: dict) -> dict:
    """Derive scope from the token, embed, and run the scoped QueryVectors.

    The body carries the `idp_token`, the query (`query` for text, or `image_b64` +
    `image_format` for multimodal), an optional `index_kind` ("text"|"mm"), and an
    optional `top_k`. Tenant, scope, courses, and the index name are ALL derived from
    the verified token — any `tenant`/`scope`/`filter`/`index` field in the body is
    ignored (SEC: a client cannot widen its own retrieval scope). The scope filter is
    injected for BOTH index kinds (#94 closes the multimodal bypass left by #84)."""
    if not VECTOR_READER_ROLE_ARN or not VECTOR_BUCKET:
        raise RetrievalError("retrieval proxy misconfigured")

    claims = validate_idp_token(req.get("idp_token", ""))
    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        raise RetrievalError(f"cannot scope session: {exc}") from exc

    top_k = req.get("top_k", DEFAULT_TOP_K)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        top_k = DEFAULT_TOP_K
    top_k = min(top_k, MAX_TOP_K)

    subject = str(claims.get("sub") or claims.get("subject") or "agate-user")
    # Scope filter from the VERIFIED token — the load-bearing line of #84/#94. Applied
    # to whichever index we query, so sub-tenant scope holds for text AND multimodal.
    vfilter = scope_filter(retrieval_nodes(tags.scope, tags.courses))

    index_kind = req.get("index_kind", "text")
    if index_kind == "mm":
        index_name = mm_index_name_for_tenant(tags.tenant)
        vector = _embed_mm_from_request(req)
    elif index_kind == "text":
        query = req.get("query")
        if not query or not isinstance(query, str):
            raise RetrievalError("request missing query")
        index_name = index_name_for_tenant(tags.tenant)
        vector = embed_query(query)
    else:
        raise RetrievalError(f"unknown index_kind: {index_kind}")

    vectors_client = assume_vector_reader(tags, subject)
    resp = vectors_client.query_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=index_name,
        topK=top_k,
        queryVector={"float32": vector},
        filter=vfilter,
        returnMetadata=True,
        returnDistance=True,
    )
    if index_kind == "mm":
        return {"matches": [_mm_match(v) for v in resp.get("vectors", [])]}
    return {"chunks": [c for c in (_text_chunk(v) for v in resp.get("vectors", [])) if c]}


def _embed_mm_from_request(req: dict) -> list[float]:
    """Embed a multimodal query from the request (text OR image). The query CONTENT is
    legitimately client-supplied (it's what the user is searching for); only the
    tenant/scope/index — the access boundary — come from the token, never the body."""
    text = req.get("query")
    image_b64 = req.get("image_b64")
    image_format = req.get("image_format")
    has_text = isinstance(text, str) and bool(text)
    has_image = isinstance(image_b64, str) and bool(image_b64)
    if has_text == has_image:  # need exactly one
        raise RetrievalError("multimodal query needs exactly one of query or image_b64")
    try:
        return embed_mm_query(
            text if has_text else None,
            image_b64 if has_image else None,
            image_format if has_image else None,
        )
    except ValueError as exc:  # bad image_format etc. -> 4xx, not 500
        raise RetrievalError(str(exc)) from exc


def _text_chunk(v: dict) -> dict | None:
    """Map one text vector to a context chunk; None if it has no usable text."""
    md = v.get("metadata") or {}
    text = md.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    src = md.get("source_key")
    # Connector provenance (#133): a chunk ingested via a connector cites its source SYSTEM
    # + item, not just the agate S3 key. Absent for ordinary uploads.
    src_system = md.get("source_system")
    src_item = md.get("source_item")
    return {
        "key": v.get("key", ""),
        "text": text,
        "sourceKey": src if isinstance(src, str) else None,
        "sourceSystem": src_system if isinstance(src_system, str) else None,
        "sourceItem": src_item if isinstance(src_item, str) else None,
        "distance": v.get("distance"),
    }


def _mm_match(v: dict) -> dict:
    """Map one multimodal vector to a visual match (mirrors the SPA elementFromMetadata
    shape: source/modality/ref/thumb + distance)."""
    md = v.get("metadata") or {}
    src = md.get("source_key")
    source_id = src if isinstance(src, str) else v.get("key", "")
    return {
        "sourceId": source_id,
        "modality": md.get("modality") if isinstance(md.get("modality"), str) else "text",
        "ref": md.get("ref") if isinstance(md.get("ref"), str) else "",
        "thumb": md.get("thumb") if isinstance(md.get("thumb"), str) else None,
        "distance": v.get("distance"),
    }


def handler(event: dict, context: object) -> dict:
    """Function URL entry point. POST {idp_token, query, top_k?}. Fail-closed: a
    scoping/verification failure returns 403 with no results, never a broad set."""
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        req = json.loads(body) if isinstance(body, str) else body
        return _resp(200, process(req))
    except RetrievalError as exc:
        return _resp(403, {"error": "not_entitled", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        # Log the traceback (CloudWatch) so a 500 is diagnosable; the response body
        # stays opaque (no internals leaked, no partial/unscoped results). Unchanged.
        logging.exception("retrieval_error")
        return _resp(500, {"error": "retrieval_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
