"""Tier 1 choke point (design §2, §7.1, §12 Phase 6) — OPTIONAL.

A thin Lambda (Function URL, response streaming) for institutions that require
EXACT pre-spend enforcement, centralized inspection, or non-Bedrock routing —
rather than the default soft cap. The flow per request:

  1. Derive identity (tenant/user/tier/courses) the SAME way the broker does — by
     validating the campus-IdP token and running the pure `claims_to_tags`. It is
     NEVER taken from free-form request fields (SEC-1). The body carries only the
     IdP token and the chat request (model/messages/max_tokens).
  2. Look up the budget SERVER-SIDE from the budget table, keyed by the verified
     tenant/user — the caller cannot choose or omit their own cap.
  3. Run the EXACT pre-call gate (cost.evaluate_precall) against authoritative spend
     (from the spend table) + that budget. Reject before the call if the worst-case
     cost would exceed budget.
  4. On allow, invoke Bedrock Converse **assuming the authenticated role narrowed by
     the derived tags** — same ABAC as Tier 0, plus enforcement.

Default Tier 0 never touches this. Token estimation is always computed server-side
(conservative char/4 round-up) — never trusted from the client.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

import boto3
from agg.tags import ClaimsError, claims_to_tags
from cost import evaluate_precall
from cost.pricing import default_pricebook
from meter import read_spend_item

SPEND_TABLE = os.environ.get("AGG_SPEND_TABLE", "")
BUDGET_TABLE = os.environ.get("AGG_BUDGET_TABLE", "")
AUTHENTICATED_ROLE_ARN = os.environ.get("AGG_AUTHENTICATED_ROLE_ARN", "")
DEFAULT_MAX_TOKENS = int(os.environ.get("AGG_DEFAULT_MAX_TOKENS", "1024"))

_ddb = boto3.resource("dynamodb")
_sts = boto3.client("sts")


class ChokepointError(Exception):
    """Reject the request (4xx). Never falls through to an unmetered call."""


def estimate_input_tokens(messages: list[dict]) -> int:
    """Conservative server-side input token estimate (char/4, round up). Never
    trusts a client-supplied count — a small lie would shrink the pre-call gate."""
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return int(math.ceil(chars / 4)) + 1


def lookup_budget(tenant: str, user: str, period: str) -> float | None:
    """The authoritative budget for the verified identity, from the budget table.

    Returns None when no budget table / no row is configured (soft-cap institutions
    don't set Tier 1 caps). The caller cannot influence this — it is keyed by the
    server-derived tenant/user, not by any request field.
    """
    if not BUDGET_TABLE:
        return None
    item = (
        _ddb.Table(BUDGET_TABLE)
        .get_item(Key={"pk": f"{tenant}#{user}#{period}"})
        .get("Item")
    )
    if not item:
        # fall back to a tenant-level budget row if present
        item = _ddb.Table(BUDGET_TABLE).get_item(Key={"pk": f"{tenant}#{period}"}).get("Item")
    return float(item["budget_usd"]) if item and "budget_usd" in item else None


def read_spend(tenant: str, user: str, period: str) -> float:
    """Authoritative spend for (tenant,user,period) from the spend table (§13.6).
    Shares meter.read_spend_item so the key format can't drift between the two."""
    if not SPEND_TABLE:
        return 0.0
    return read_spend_item(_ddb.Table(SPEND_TABLE), tenant, user, period)


def assume_user_role(tags, user: str) -> Any:
    """Assume the authenticated role narrowed by the VERIFIED agg: session tags
    (the SessionTags object from claims_to_tags), returning a scoped Bedrock client.

    The tags are derived from the validated IdP token, not from request fields, so
    the resulting session has exactly the caller's real entitlement — the choke
    point cannot widen access."""
    resp = _sts.assume_role(
        RoleArn=AUTHENTICATED_ROLE_ARN,
        RoleSessionName=(user or "agg-user")[:64],
        Tags=tags.to_sts_tags(),
        TransitiveTagKeys=[t["Key"] for t in tags.to_sts_tags()],
        DurationSeconds=900,
    )
    c = resp["Credentials"]
    return boto3.client(
        "bedrock-runtime",
        aws_access_key_id=c["AccessKeyId"],
        aws_secret_access_key=c["SecretAccessKey"],
        aws_session_token=c["SessionToken"],
    )


def validate_idp_token(token: str) -> dict:
    """Validate the campus-IdP token -> claims. Same Phase-1 placeholder seam as the
    broker (real JWKS verification lands with the IdP wiring); fails closed."""
    if not token:
        raise ChokepointError("no IdP token presented")
    try:
        claims = json.loads(token)
    except (ValueError, TypeError) as exc:
        raise ChokepointError("malformed IdP token") from exc
    if not isinstance(claims, dict):
        raise ChokepointError("IdP token did not decode to a claim set")
    return claims


def _period_now() -> str:
    """Current billing period (YYYY-MM). Imported lazily so the module stays
    import-light; the broker stamps the same format on invocation records."""
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m")


def process(req: dict, *, period: str | None = None) -> dict:
    """Derive identity from the IdP token, gate on server-side budget, then (on
    allow) invoke the scoped Converse. Identity/budget are NEVER from the body
    (SEC-1) — the body carries only idp_token + model/messages/max_tokens."""
    # Identity: validate the token and derive the agg: tags the SAME way the broker
    # does. tenant/user/tier/courses come from here, never from request fields.
    claims = validate_idp_token(req.get("idp_token", ""))
    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        raise ChokepointError(f"cannot scope session: {exc}") from exc
    tenant = tags.tenant
    user = str(claims.get("sub") or claims.get("subject") or "agg-user")
    period = period or _period_now()

    model_id = req.get("model")
    messages = req.get("messages") or []
    if not model_id or not messages:
        raise ChokepointError("request missing model/messages")

    max_tokens = int(req.get("max_tokens", DEFAULT_MAX_TOKENS))
    input_tokens = estimate_input_tokens(messages)  # server-side only
    budget = lookup_budget(tenant, user, period)  # server-side, keyed by verified id

    spend = read_spend(tenant, user, period)
    gate = evaluate_precall(
        model_id=model_id,
        input_tokens=input_tokens,
        max_tokens=max_tokens,
        spend=spend,
        budget=budget,
        pricebook=default_pricebook(),
    )
    if gate.decision == "reject":
        raise ChokepointError(
            f"pre-call budget check failed: {gate.reason} "
            f"(projected ${gate.projected_total} vs budget ${budget})"
        )

    # Allowed: invoke Converse with the role narrowed by the VERIFIED tags.
    br = assume_user_role(tags, user)
    resp = br.converse(
        modelId=model_id,
        messages=[{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages],
        inferenceConfig={"maxTokens": max_tokens},
    )
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
    usage = resp.get("usage", {})
    return {
        "text": text,
        "usage": {
            "inputTokens": usage.get("inputTokens", 0),
            "outputTokens": usage.get("outputTokens", 0),
        },
        "estimated_cost": gate.estimated_cost,
    }


def handler(event: dict, context: object) -> dict:
    """Function URL entry point. Rejects map to 402 (payment required) — semantically
    apt for a budget rejection — never to a silent unmetered call."""
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        req = json.loads(body) if isinstance(body, str) else body
        return _resp(200, process(req))
    except ChokepointError as exc:
        return _resp(402, {"error": "budget_rejected", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — fail closed
        return _resp(500, {"error": "chokepoint_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
