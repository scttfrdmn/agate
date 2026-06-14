"""Governed-access console API — admin-gated usage/spend analytics + budget writes.

The differentiator vs both NebulaONE ("usage limits per user") and Amazon Quick
(no per-capita entitlement): agate already derives a verified `agate:role` from the
campus token, so the admin surface is gated at the SAME credential boundary as
everything else — not an app-layer check.

Two operations, both admin-gated at the credential boundary:
  * default (no `op`) — READ: scan the spend table, return the console rollups
    (agate.admin, pure).
  * `op: "set_budget"` — WRITE (#87): author a budget row in `agate-budget` in the
    EXACT key shape the chokepoint reads (agate.budget, pure). A scoped admin may
    only write within their own subtree; cross-tenant writes are impossible.

Flow (fail-closed at every step):
  1. Verify the inbound IdP token (real RS256/JWKS, shared agate.jwt_verify).
  2. Derive tags; require role == admin. Anything else -> 403, no data, no write.
  3. Dispatch on `op`; identity (tenant/admin_scope) comes from the VERIFIED token,
     never the request body.

Per-request Lambda behind the same HTTP API pattern as the broker. No clock.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import boto3
from agate.admin import to_console_payload
from agate.budget import BudgetError, plan_budget_write
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.tags import ROLE_ADMIN, ClaimsError, claims_to_tags

SPEND_TABLE = os.environ.get("AGATE_SPEND_TABLE", "")
BUDGET_TABLE = os.environ.get("AGATE_BUDGET_TABLE", "")

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


def set_budget(tags, payload: dict) -> dict:
    """Write one budget row (#87). The target/amount come from the body but identity
    (tenant + admin_scope) comes from the VERIFIED token, so a scoped admin cannot
    escape their subtree and no one can write another tenant. The pure
    `plan_budget_write` does all validation/authorization + builds the exact key the
    chokepoint reads; here we only PUT it."""
    if not BUDGET_TABLE:
        raise AdminError("admin API misconfigured: no budget table")
    try:
        write = plan_budget_write(
            actor_tenant=tags.tenant,
            actor_admin_scope=tags.admin_scope,
            tenant=str(payload.get("tenant", "")),
            usd=payload.get("usd"),
            period=str(payload.get("period", "")),
            scope=payload.get("scope"),
            user=payload.get("user"),
        )
    except BudgetError as exc:
        # A validation/authorization rejection is the caller's fault -> 4xx, not 500.
        raise AdminError(str(exc)) from exc
    # Decimal: DynamoDB rejects float; mirror meter.handler._increment's Decimal use.
    _ddb.Table(BUDGET_TABLE).put_item(
        Item={"pk": write.pk, "budget_usd": Decimal(str(write.budget_usd))}
    )
    return {
        "ok": True,
        "pk": write.pk,
        "budget_usd": write.budget_usd,
        "tenant": write.tenant,
        "period": write.period,
        "scope": write.scope,
        "user": write.user,
    }


def handler(event: dict, context: object) -> dict:
    """API Gateway v2 (HTTP API) entry point.
    POST {idp_token, period?}                       -> spend analytics (read)
    POST {idp_token, op:"set_budget", tenant, usd, period, scope?|user?} -> write (#87)
    """
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        payload = json.loads(body) if isinstance(body, str) else body

        # Admin gate first — no op runs for a non-admin (or unverifiable) token.
        tags = require_admin(payload.get("idp_token", ""))

        op = payload.get("op")
        if op == "set_budget":
            return _resp(200, set_budget(tags, payload))
        if op not in (None, "spend", "analytics"):
            raise AdminError(f"unknown op: {op}")

        if not SPEND_TABLE:
            raise AdminError("admin API misconfigured: no spend table")
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
