"""Deploy-on-confirm endpoint (#118 last slice) — persist a confirmed, clamped agent.

The final step of natural-language authoring: the SPA confirms a drafted plan and POSTs the
validated spec here; this endpoint CREATES the agent. Per §0.1, "create" = persist the
governed spec record (agate governs; the runtime/agenkit re-instantiates + runs it). No
standing credential is vended — a created agent is its spec under a scope-fenced key.

THE LOAD-BEARING RULE (the #130 lesson): this endpoint does NOT trust the client's spec as
authority. It RE-RUNS `dispose_draft` against the VERIFIED token, so the persisted spec is
re-clamped to the author's own entitlement server-side — a tampered/over-broad echoed spec is
clamped down or rejected exactly as a fresh draft would be. The tenant/scope the record is
keyed under come from the re-clamped `child_tags`, never from the request body.

The write itself goes through a tenant-fenced role the handler ASSUMES with the verified
`agate:` session tags (the #84/#130 pattern): so the principal that PUTs the object carries the
tenant tag the bucket policy's `${aws:PrincipalTag/...}` fences — the broadly-vended browser
role stays read-only. Per-request Lambda behind an IAM-authed Function URL, NO CLOCKS. Fails
closed.
"""

from __future__ import annotations

import json
import os
import time

import boto3
from agate.agent_record import agent_object_key, build_agent_record
from agate.agentspec import parse_spec
from agate.drafting import dispose_draft
from agate.identity import agent_id, spec_version
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.tags import ClaimsError, SessionTags, claims_to_tags, role_session_name

REGION = os.environ.get("AGATE_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
DOCS_BUCKET = os.environ.get("AGATE_DOCS_BUCKET", "")
# The tenant-fenced role the handler assumes (with the session's agate: tags) to PUT the
# agent record — so the writing principal carries the tenant tag the bucket policy fences.
# The Lambda's own role only gets sts:AssumeRole on this role (mirrors the #130 memory tool).
DEPLOY_ROLE_ARN = os.environ.get("AGATE_AGENT_DEPLOY_ROLE_ARN", "")

_sts = boto3.client("sts", region_name=REGION)


class DeployError(ValueError):
    """A deploy request that cannot be served safely. Fail closed."""


def validate_idp_token(token: str) -> dict:
    """Verify the campus-IdP token (real RS256/JWKS) — the SAME verifier the broker, retrieval
    proxy, drafting, and slurm use. The inbound identity is the verified author."""
    if not token or not isinstance(token, str):
        raise DeployError("missing idp_token")
    try:
        return verify_token(token, **config_from_env())
    except TokenError as exc:
        raise DeployError(f"token verification failed: {exc}") from exc


def _assume_writer(tags: SessionTags, subject: str):
    """Assume the tenant-fenced deploy role with the verified `agate:` session tags, returning
    a scoped S3 client. The tenant/scope tags travel on the assumed session, so the bucket
    policy's `${aws:PrincipalTag/...}` fence binds the credential that writes — the #84/#130
    discipline (never the Lambda's own un-tagged role)."""
    if not DEPLOY_ROLE_ARN:
        raise DeployError("AGATE_AGENT_DEPLOY_ROLE_ARN not configured")
    sts_tags = tags.to_sts_tags()
    resp = _sts.assume_role(
        RoleArn=DEPLOY_ROLE_ARN,
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


def process(req: dict) -> dict:
    """Re-clamp → persist. `req` carries the verified `idp_token` and the `spec` dict the SPA
    confirmed. The spec is re-disposed against the verified token (re-clamped server-side), then
    persisted under the re-clamped tenant/scope. The echoed spec is NEVER trusted as authority —
    a tampered one is clamped/rejected like a fresh draft."""
    if not DOCS_BUCKET:
        raise DeployError("AGATE_DOCS_BUCKET not configured")
    claims = validate_idp_token(req.get("idp_token", ""))
    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        raise DeployError(f"cannot scope session: {exc}") from exc
    subject = str(claims.get("sub") or claims.get("subject") or "agate-user")

    spec_dict = req.get("spec")
    if not isinstance(spec_dict, dict) or not spec_dict:
        raise DeployError("missing spec")

    # RE-CLAMP server-side: the same disposer the draft used, against the VERIFIED token. The
    # outcome's instance carries the author-narrowed credential — the source of the record's
    # tenant/scope (never the client). A tampered/over-broad spec fails closed here.
    outcome = dispose_draft(spec_dict, tags, subject=subject)
    if not outcome.ok or outcome.instance is None:
        return {"ok": False, "reason": outcome.reason or "draft could not be bounded"}

    child = outcome.instance.child_tags  # the re-clamped credential — authority, not the body
    spec = parse_spec(spec_dict)  # validated again (dispose already parsed; explicit for name)
    key = agent_object_key(child.tenant, child.scope, spec.name)
    record = build_agent_record(
        name=spec.name,
        tenant=child.tenant,
        scope=child.scope,
        agent_id=agent_id(child.tenant, spec.name),
        spec_version=spec_version(spec),
        created_by=role_session_name(child.tenant, subject),  # verified <tenant>@<subject>
        created=_now_iso(),
        spec=spec_dict,
        boundary=outcome.summary(),
    )

    s3 = _assume_writer(tags, subject)
    s3.put_object(
        Bucket=DOCS_BUCKET,
        Key=key,
        Body=record.to_json().encode("utf-8"),
        ContentType="application/json",
    )
    return {
        "ok": True,
        "agent_id": record.agent_id,
        "spec_version": record.spec_version,
        "key": key,
        "plan": list(record.boundary),
    }


def _now_iso() -> str:
    """ISO-8601 UTC stamp — the live-edge record time (the pure agent_record module stays
    clockless; the server caller stamps, exactly as the SavedSession persist path does)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def handler(event: dict, context: object) -> dict:
    """Function URL entry point. Fail-closed: a verification/scoping failure returns an error
    envelope, never a silent write."""
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        req = json.loads(body) if isinstance(body, str) else body
        return _resp(200, process(req))
    except DeployError as exc:
        return _resp(403, {"error": "not_entitled", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        return _resp(500, {"error": "deploy_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
