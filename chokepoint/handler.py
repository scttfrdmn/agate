"""Tier 1 choke point (design §2, §7.1, §12 Phase 6) — OPTIONAL.

A thin Lambda (Function URL, response streaming) for institutions that require
EXACT pre-spend enforcement, centralized inspection, or non-Bedrock routing —
rather than the default soft cap. The flow per request:

  1. Read the OpenAI-style chat request (model, messages, max_tokens) + the
     federated subject's tenant/user (from the validated session the SPA presents).
  2. Run the EXACT pre-call gate (cost.evaluate_precall) against authoritative spend
     (read from the spend table) + the tenant budget. Reject before the call if the
     worst-case cost would exceed budget.
  3. On allow, invoke Bedrock Converse **assuming the user's own scoped role** so the
     same ABAC model scope applies — the choke point adds enforcement, it does not
     widen access.

Default Tier 0 never touches this. The handler is wired only when an institution
opts into Tier 1. Token estimation uses a fast char/4 heuristic when the client
doesn't supply input_tokens — deliberately conservative (rounds up).
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

import boto3
from cost import evaluate_precall
from cost.pricing import default_pricebook
from meter import read_spend_item

SPEND_TABLE = os.environ.get("AGG_SPEND_TABLE", "")
AUTHENTICATED_ROLE_ARN = os.environ.get("AGG_AUTHENTICATED_ROLE_ARN", "")
DEFAULT_MAX_TOKENS = int(os.environ.get("AGG_DEFAULT_MAX_TOKENS", "1024"))

_ddb = boto3.resource("dynamodb")
_sts = boto3.client("sts")


class ChokepointError(Exception):
    """Reject the request (4xx). Never falls through to an unmetered call."""


def estimate_input_tokens(messages: list[dict], explicit: int | None) -> int:
    """Input token count: trust an explicit value, else a conservative char/4 + 1."""
    if explicit is not None and explicit >= 0:
        return explicit
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return int(math.ceil(chars / 4)) + 1


def read_spend(tenant: str, user: str, period: str) -> float:
    """Authoritative spend for (tenant,user,period) from the spend table (§13.6).
    Shares meter.read_spend_item so the key format can't drift between the two."""
    if not SPEND_TABLE:
        return 0.0
    return read_spend_item(_ddb.Table(SPEND_TABLE), tenant, user, period)


def assume_user_role(tenant: str, user: str, tier: str, courses: list[str]) -> Any:
    """Assume the authenticated role narrowed by the user's agg: tags, returning a
    Bedrock client scoped exactly as Tier 0 would be (the choke point does not
    widen access — same ABAC, plus pre-call enforcement)."""
    tags = [
        {"Key": "agg:tenant", "Value": tenant},
        {"Key": "agg:tier", "Value": tier},
        {"Key": "agg:courses", "Value": ",".join(courses)},
    ]
    resp = _sts.assume_role(
        RoleArn=AUTHENTICATED_ROLE_ARN,
        RoleSessionName=user[:64] or "agg-user",
        Tags=tags,
        DurationSeconds=900,
    )
    c = resp["Credentials"]
    return boto3.client(
        "bedrock-runtime",
        aws_access_key_id=c["AccessKeyId"],
        aws_secret_access_key=c["SecretAccessKey"],
        aws_session_token=c["SessionToken"],
    )


def process(req: dict) -> dict:
    """Run the pre-call gate, then (on allow) the scoped Converse. Pure-ish: all
    AWS calls go through the module clients, which tests stub."""
    tenant = req.get("tenant")
    user = req.get("user")
    period = req.get("period")
    if not (tenant and user and period):
        raise ChokepointError("request missing tenant/user/period")

    model_id = req.get("model")
    messages = req.get("messages") or []
    if not model_id or not messages:
        raise ChokepointError("request missing model/messages")

    max_tokens = int(req.get("max_tokens", DEFAULT_MAX_TOKENS))
    input_tokens = estimate_input_tokens(messages, req.get("input_tokens"))
    budget = req.get("budget")  # None => no cap (soft-cap institutions don't set it)

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

    # Allowed: invoke Converse with the user's own scoped role.
    br = assume_user_role(tenant, user, req.get("tier", "oss"), req.get("courses") or [])
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
