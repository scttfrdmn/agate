"""AgentCore Memory read/write server (#130 / #110) — the SDK path behind the 3 tiers.

The EFFECT half of the §5 split for memory: #110's IAM (`memory_access_policy`) already
fenced WHICH namespaces a credential may touch; this server makes the actual `create_event`
(record) / `retrieve_memory_records` (recall) calls — and the load-bearing rule is that
**every namespace it touches comes from `agate.memory.namespaces_for`, derived from the
VERIFIED identity, never from the tool payload.** A client can ask to record/recall, but it
can never name a namespace, an actor, a tenant, or a scope — those are the verified
credential's, exactly as the slurm/retrieval edges do it.

Two ops:
  * `record` (write): `create_event` under a server-derived `actorId` (tenant-qualified +
    injective `subject_key`) and the verified `sessionId`. AgentCore resolves the namespace
    from the actor + the resource's strategy; the actorId is tenant-qualified so the resolved
    path stays inside the `agate/{tenant}/...` subtree the IAM policy fences.
  * `recall` (read): `retrieve_memory_records` with an explicit `namespacePath` taken from
    `namespaces_for(tags, subject, session_id)[tier]` — the path is fully agate-controlled and
    is exactly what the live `SimulateCustomPolicy` proof (#110) shows IAM allows/denies. A
    `tier` the session doesn't have (e.g. `shared` when unscoped) is rejected: `namespaces_for`
    omits it, so it's never in the derived dict.

Per-request Lambda, no clock in the pure logic (the event timestamp is the live edge's, like
any record-time stamp). Fails closed: any verification/scoping error returns an error
envelope, never a silent broad action.
"""

from __future__ import annotations

import json
import os
import time

import boto3
from agate.delegate import subject_key
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.memory import MemoryTier, namespaces_for
from agate.tags import ClaimsError, SessionTags, claims_to_tags, role_session_name

REGION = os.environ.get("AGATE_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
MEMORY_ID = os.environ.get("AGATE_MEMORY_ID", "")
# The tenant-fenced role the handler ASSUMES (with the session's agate: tags) before touching
# AgentCore — so the calling principal carries `agate:tenant`/`agate:scope` and the
# `memory_access_policy` namespacePath fence is actually operative. Mirrors the #84 retrieval
# proxy: the IAM fence depends on the principal CARRYING the tags, so the Lambda's own
# (un-tagged) role must NOT be the one that calls AgentCore.
MEMORY_ACCESS_ROLE_ARN = os.environ.get("AGATE_MEMORY_ACCESS_ROLE_ARN", "")

# Module-level STS client (reused across warm invocations); the AgentCore client is built
# per-request from the assumed, tag-scoped credentials (it cannot be a module singleton —
# each request carries a different tenant/scope).
_sts = boto3.client("sts", region_name=REGION)

_VALID_TIERS: tuple[MemoryTier, ...] = ("session", "personal", "shared")


class MemoryToolError(ValueError):
    """A memory call that cannot be served safely. Fail closed."""


def assume_memory_client(tags: SessionTags, subject: str):
    """Assume the tenant-fenced memory role, narrowed by the VERIFIED `agate:` session tags,
    and return an AgentCore client built from those scoped credentials. The tenant + scope
    tags travel on the assumed session, so `memory_access_policy`'s `${aws:PrincipalTag/...}`
    `namespacePath` condition fences the credential itself (#110) — exactly the #84 pattern.
    The RoleSessionName ties the call to the federated subject + tenant (#79)."""
    if not MEMORY_ACCESS_ROLE_ARN:
        raise MemoryToolError("AGATE_MEMORY_ACCESS_ROLE_ARN not configured")
    sts_tags = tags.to_sts_tags()
    resp = _sts.assume_role(
        RoleArn=MEMORY_ACCESS_ROLE_ARN,
        RoleSessionName=role_session_name(tags.tenant, subject),
        Tags=sts_tags,
        TransitiveTagKeys=[t["Key"] for t in sts_tags],
        DurationSeconds=900,
    )
    c = resp["Credentials"]
    return boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        aws_access_key_id=c["AccessKeyId"],
        aws_secret_access_key=c["SecretAccessKey"],
        aws_session_token=c["SessionToken"],
    )


def validate_idp_token(token: str) -> dict:
    """Verify the campus-IdP token (real RS256/JWKS) — the SAME verifier the broker, retrieval
    proxy, and slurm server use. The inbound identity is the verified user the agent acts for."""
    if not token or not isinstance(token, str):
        raise MemoryToolError("missing idp_token")
    try:
        return verify_token(token, **config_from_env())
    except TokenError as exc:
        raise MemoryToolError(f"token verification failed: {exc}") from exc


def _actor_id(tags: SessionTags, subject: str) -> str:
    """The AgentCore `actorId` for a write — `{tenant}@{subject_key}`, so the namespace
    AgentCore resolves from it stays inside the credential's `agate/{tenant}/...` fence. Tenant
    FIRST so two tenants' actors can never collide; `subject_key` makes the per-principal
    segment injective (a collision here would be a cross-principal memory leak). The `@`
    separator is the same unforgeable tenant delimiter `role_session_name` uses — `@` is
    excluded from the tenant grammar (`tags._TENANT_RE`), so `{tenant}@{...}` parses back
    unambiguously (a `.` would not: `.` is legal in both tenant and subject_key)."""
    return f"{tags.tenant}@{subject_key(subject)}"


def _session_id(req: dict) -> str:
    """The one client-supplied value — the conversation id. Sanitised to the namespace
    segment grammar (the same `_seg` `agate.memory` uses); fail-closed if empty. It only ever
    selects a child UNDER the verified principal's tree, never escapes it."""
    raw = str(req.get("session_id") or "").strip()
    # `namespaces_for` will sanitise it into the path; reject obviously-empty up front so a
    # blank session can't silently collapse to the personal namespace.
    if not raw:
        raise MemoryToolError("missing session_id")
    return raw


def _identity(req: dict) -> tuple[SessionTags, str]:
    """Verify the token and derive (tags, subject) — both from the verified claims, never the
    body. Subject is the IdP `sub`, exactly as the slurm edge derives it."""
    claims = validate_idp_token(req.get("idp_token", ""))
    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        raise MemoryToolError(f"cannot scope session: {exc}") from exc
    subject = str(claims.get("sub") or claims.get("subject") or "agate-user")
    return tags, subject


def record(req: dict) -> dict:
    """`record`: append a conversation event to the caller's OWN memory. The actor/session are
    server-derived; the only client content is the `payload` (the turn to remember), which
    carries NO identity/namespace — those are the verified credential's."""
    tags, subject = _identity(req)
    session_id = _session_id(req)
    payload = req.get("payload")
    if not isinstance(payload, list) or not payload:
        raise MemoryToolError("record requires a non-empty payload list")
    # Derive the namespaces this session may touch (proves the session_id resolves to a real
    # in-fence path; also the value a follow-up recall will read).
    namespaces = namespaces_for(tags, subject, session_id)
    client = assume_memory_client(tags, subject)  # tag-scoped; the IAM fence is operative
    resp = client.create_event(
        memoryId=MEMORY_ID,
        actorId=_actor_id(tags, subject),
        sessionId=session_id,
        eventTimestamp=time.time(),  # live edge record-time; pure logic stays clockless
        payload=payload,
    )
    return {
        "recorded": True,
        "eventId": resp.get("event", {}).get("eventId") or resp.get("eventId"),
        "namespaces": namespaces,  # echo the server-derived paths (never a client value)
    }


def recall(req: dict) -> dict:
    """`recall`: read memory records from ONE tier the caller actually has. The namespacePath
    is taken from `namespaces_for` — never the body — so a client can't read another tenant,
    principal, or scope (the #110 IAM fence backs this; this is the path that fence guards)."""
    tags, subject = _identity(req)
    session_id = str(req.get("session_id") or "").strip()
    tier = req.get("tier")
    if tier not in _VALID_TIERS:
        raise MemoryToolError(f"unknown tier: {tier!r}")
    # `session` recall needs a session id; `personal`/`shared` don't.
    if tier == "session" and not session_id:
        raise MemoryToolError("session recall requires session_id")
    namespaces = namespaces_for(tags, subject, session_id or "_")
    if tier not in namespaces:
        # e.g. `shared` on an unscoped session — namespaces_for omits it. Fail closed.
        raise MemoryToolError(f"tier not available for this session: {tier}")
    query = str(req.get("query") or "")
    max_results = int(req.get("max_results") or 20)
    client = assume_memory_client(tags, subject)  # tag-scoped; the IAM fence is operative
    resp = client.retrieve_memory_records(
        memoryId=MEMORY_ID,
        namespace=namespaces[tier],
        searchCriteria={"searchQuery": query} if query else {"searchQuery": ""},
        maxResults=max_results,
    )
    return {
        "tier": tier,
        "namespace": namespaces[tier],  # the server-derived path (never a client value)
        "records": resp.get("memoryRecordSummaries") or resp.get("records") or [],
    }


def process(req: dict) -> dict:
    """Route one memory tool call. `req` carries the verified `idp_token`, an `op`
    (`record`|`recall`), and op args. Identity/namespace are ALL server-derived — any
    `namespace`/`actorId`/`tenant`/`scope` in the body is ignored."""
    op = req.get("op")
    if op == "record":
        return record(req)
    if op == "recall":
        return recall(req)
    raise MemoryToolError(f"unknown op: {op!r}")


def handler(event: dict, context: object) -> dict:
    """Lambda entry point. Fail-closed: a verification/scoping failure returns an error
    envelope, never a silent broad action."""
    try:
        if not MEMORY_ID:
            raise MemoryToolError("AGATE_MEMORY_ID not configured")
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        req = json.loads(body) if isinstance(body, str) else body
        return _resp(200, process(req))
    except MemoryToolError as exc:
        return _resp(403, {"error": "not_entitled", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        return _resp(500, {"error": "memory_tool_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
