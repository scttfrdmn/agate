"""Governed-access console API — admin-gated usage/spend analytics (Track 1, #63).

The differentiator vs both NebulaONE ("usage limits per user") and Amazon Quick
(no per-capita entitlement): agate already derives a verified `agate:role` from the
campus token, so the admin surface is gated at the SAME credential boundary as
everything else — not an app-layer check.

Flow (fail-closed at every step):
  1. Verify the inbound IdP token (real RS256/JWKS, shared agate.jwt_verify).
  2. Derive tags; require role == admin. Anything else -> 403, no data.
  3. Scan the authoritative spend table and return the rollups the console renders
     (agate.admin, pure). Read-only — this slice vends analytics, not writes.

Per-request Lambda behind the same HTTP API pattern as the broker. No clock.
"""

from __future__ import annotations

import json
import os

import boto3
from agate.admin import to_console_payload
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.tags import ROLE_ADMIN, ClaimsError, claims_to_tags

SPEND_TABLE = os.environ.get("AGATE_SPEND_TABLE", "")

_ddb = boto3.resource("dynamodb")


class AdminError(Exception):
    """Return-no-data error -> mapped to a terse 4xx, never an analytics payload."""


def require_admin(token: str) -> None:
    """Verify the token and require the verified `agate:role` to be admin.

    Returns the verified SessionTags (so the caller can read `tenant`/`admin_scope`
    to narrow what's shown). Raises AdminError on any failure — unverifiable token,
    malformed claims, or a non-admin role. Admin is never inferred from a request
    field; it is the role derived from the verified token (same path the broker uses).
    """
    cfg = config_from_env()
    try:
        claims = verify_token(token, **cfg)
        tags = claims_to_tags(claims)
    except (TokenError, ClaimsError) as exc:
        raise AdminError(f"not authorized: {exc}") from exc
    if tags.role != ROLE_ADMIN:
        raise AdminError("not authorized: admin role required")
    return tags


def _scan_spend(table) -> list[dict]:
    """Scan all spend rows (paginated). The table is per-deployment and small
    (one row per tenant/user/period); a scan is appropriate here.

    The spend table is owned by `agate-audit`. If that stack isn't deployed yet the
    table won't exist — that's a legitimate "no usage recorded" state, not an error,
    so we degrade to empty analytics rather than 500.
    """
    items: list[dict] = []
    kwargs: dict = {}
    try:
        while True:
            resp = table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
    except _ddb.meta.client.exceptions.ResourceNotFoundException:
        return []
    return items


def handler(event: dict, context: object) -> dict:
    """API Gateway v2 (HTTP API) entry point. POST {idp_token, period?}."""
    try:
        if not SPEND_TABLE:
            raise AdminError("admin API misconfigured: no spend table")
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        payload = json.loads(body) if isinstance(body, str) else body

        tags = require_admin(payload.get("idp_token", ""))

        # A SCOPED admin (admin_scope set) governs their own tenant only; a
        # tenant-wide/global admin (no admin_scope) sees every tenant. (#70 RBAC,
        # app-level — subtree-granular spend awaits scope-keyed spend rows in the
        # budget-cascade phase.)
        only_tenant = tags.tenant if tags.admin_scope else None
        period = payload.get("period")  # optional YYYY-MM filter
        items = _scan_spend(_ddb.Table(SPEND_TABLE))
        return _resp(200, to_console_payload(items, period=period, only_tenant=only_tenant))
    except AdminError as exc:
        return _resp(403, {"error": "forbidden", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        return _resp(500, {"error": "admin_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
