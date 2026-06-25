"""Corpus endpoint (#191) — a user uploads documents into, and lists, their own
tenant+scope subtree of the docs bucket.

Two actions on one Function URL (dispatched by the body's `action`):
  * "upload" — PUT a document under `{tenant}/{scope}/{filename}`; the S3 ObjectCreated
    trigger then embeds it into the tenant's vector index (existing ingest path).
  * "list"   — enumerate the documents under the session's `{tenant}/{scope}/` prefix.

THE LOAD-BEARING RULE (the #84/#130 pattern): identity comes from the VERIFIED token,
never the request body. tenant/scope are derived via `claims_to_tags`; the S3 key/prefix
are built from those tags (`agate.corpus`), so a `tenant`/`scope`/`key` field in the body
is ignored — a user can only write/list within their own fence. The write+list go through
a tenant-fenced role the handler ASSUMES with the verified `agate:` session tags, so the
acting principal carries the tag the bucket policy's `${aws:PrincipalTag/...}` fences (the
broadly-vended browser role stays read-only). Per-request Lambda behind an IAM-authed
Function URL, NO CLOCKS. Fails closed.
"""

from __future__ import annotations

import base64
import json
import os

import boto3
from agate.corpus import CorpusKeyError, docs_list_prefix, docs_object_key
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.tags import ClaimsError, SessionTags, claims_to_tags, role_session_name

REGION = os.environ.get("AGATE_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
DOCS_BUCKET = os.environ.get("AGATE_DOCS_BUCKET", "")
# The tenant-fenced role the handler assumes (with the session's agate: tags) to
# read/write/list — the Lambda's own role only gets sts:AssumeRole on this role.
CORPUS_ROLE_ARN = os.environ.get("AGATE_CORPUS_ROLE_ARN", "")
# A conservative upload cap; ingest only embeds the first 5 MB of UTF-8 text anyway.
MAX_UPLOAD_BYTES = int(os.environ.get("AGATE_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MAX_LIST_KEYS = int(os.environ.get("AGATE_MAX_LIST_KEYS", "200"))

_sts = boto3.client("sts", region_name=REGION)


class CorpusError(ValueError):
    """A corpus request that cannot be served safely. Fail closed."""


def validate_idp_token(token: str) -> dict:
    """Verify the campus-IdP token (real RS256/JWKS) — the SAME verifier the broker,
    retrieval proxy, drafting, and deploy use."""
    if not token or not isinstance(token, str):
        raise CorpusError("missing idp_token")
    try:
        return verify_token(token, **config_from_env())
    except TokenError as exc:
        raise CorpusError(f"token verification failed: {exc}") from exc


def _assume_corpus_role(tags: SessionTags, subject: str):
    """Assume the tenant-fenced corpus role with the verified `agate:` tags, returning a
    scoped S3 client — the tenant/scope tags travel on the session, so the bucket policy's
    `${aws:PrincipalTag/...}` fence binds the credential that reads/writes/lists."""
    if not CORPUS_ROLE_ARN:
        raise CorpusError("AGATE_CORPUS_ROLE_ARN not configured")
    sts_tags = tags.to_sts_tags()
    resp = _sts.assume_role(
        RoleArn=CORPUS_ROLE_ARN,
        RoleSessionName=role_session_name(tags.tenant, subject),
        Tags=sts_tags,
        TransitiveTagKeys=[t["Key"] for t in sts_tags],
        DurationSeconds=900,
    )
    c = resp["Credentials"]
    return boto3.client(
        "s3",
        region_name=REGION,
        aws_access_key_id=c["AccessKeyId"],
        aws_secret_access_key=c["SecretAccessKey"],
        aws_session_token=c["SessionToken"],
    )


def _tags_for(req: dict) -> tuple[SessionTags, str]:
    claims = validate_idp_token(req.get("idp_token", ""))
    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        raise CorpusError(f"cannot scope session: {exc}") from exc
    subject = str(claims.get("sub") or claims.get("subject") or "agate-user")
    return tags, subject


def upload(req: dict) -> dict:
    """PUT one document under the session's verified `{tenant}/{scope}/{filename}`.
    The body carries `filename` and base64 `content` (+ optional `content_type`). The
    key is built from the VERIFIED tags + sanitised filename, never a client key field."""
    if not DOCS_BUCKET:
        raise CorpusError("AGATE_DOCS_BUCKET not configured")
    tags, subject = _tags_for(req)

    filename = req.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise CorpusError("missing filename")
    raw = req.get("content")
    if not isinstance(raw, str) or not raw:
        raise CorpusError("missing content")
    try:
        data = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise CorpusError("content is not valid base64") from exc
    if len(data) > MAX_UPLOAD_BYTES:
        raise CorpusError(f"document exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")

    try:
        key = docs_object_key(tags.tenant, tags.scope, filename)
    except CorpusKeyError as exc:
        raise CorpusError(str(exc)) from exc

    content_type = req.get("content_type")
    if not isinstance(content_type, str) or not content_type:
        content_type = "application/octet-stream"

    s3 = _assume_corpus_role(tags, subject)
    s3.put_object(Bucket=DOCS_BUCKET, Key=key, Body=data, ContentType=content_type)
    return {"ok": True, "key": key, "bytes": len(data)}


def list_docs(req: dict) -> dict:
    """List the documents under the session's verified `{tenant}/{scope}/` prefix.
    Returns name/key/size/modified, reserved `_`-namespace objects excluded."""
    if not DOCS_BUCKET:
        raise CorpusError("AGATE_DOCS_BUCKET not configured")
    tags, subject = _tags_for(req)
    try:
        prefix = docs_list_prefix(tags.tenant, tags.scope)
    except CorpusKeyError as exc:
        raise CorpusError(str(exc)) from exc

    s3 = _assume_corpus_role(tags, subject)
    resp = s3.list_objects_v2(Bucket=DOCS_BUCKET, Prefix=prefix, MaxKeys=MAX_LIST_KEYS)
    docs = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        rel = key[len(prefix) :]  # path within the scope subtree
        # Hide reserved namespaces (agents/rooms/sessions/mm-artifacts) and "folder" markers.
        if not rel or rel.endswith("/") or any(seg.startswith("_") for seg in rel.split("/")):
            continue
        last = obj.get("LastModified")
        docs.append(
            {
                "name": rel,
                "key": key,
                "size": int(obj.get("Size", 0)),
                "modified": last.isoformat() if last is not None else None,
            }
        )
    return {
        "ok": True,
        "prefix": prefix,
        "documents": docs,
        "truncated": resp.get("IsTruncated", False),
    }


def process(req: dict) -> dict:
    action = req.get("action", "list")
    if action == "upload":
        return upload(req)
    if action == "list":
        return list_docs(req)
    raise CorpusError(f"unknown action: {action}")


def handler(event: dict, context: object) -> dict:
    """Function URL entry point. Fail-closed: a verification/scoping failure returns an
    error envelope, never a silent write or a broad listing."""
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        req = json.loads(body) if isinstance(body, str) else body
        return _resp(200, process(req))
    except CorpusError as exc:
        return _resp(403, {"error": "not_entitled", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        import logging

        logging.exception("corpus_error")
        return _resp(500, {"error": "corpus_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
