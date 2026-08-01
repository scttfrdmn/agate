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
import logging
import math
import os
from decimal import Decimal
from typing import Any

import boto3
from agate.entitlements import models_for_tier, supports_vision, tier_for_model
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.rag import ancestors
from agate.router import needs_capable_model, select_model
from agate.tags import ClaimsError, claims_to_tags
from cost import estimate_call_cost, evaluate_cascade
from cost.pricing import default_pricebook
from meter import read_scope_spend_item, read_spend_item, scope_pk

SPEND_TABLE = os.environ.get("AGATE_SPEND_TABLE", "")
BUDGET_TABLE = os.environ.get("AGATE_BUDGET_TABLE", "")
AUTHENTICATED_ROLE_ARN = os.environ.get("AGATE_AUTHENTICATED_ROLE_ARN", "")
DEFAULT_MAX_TOKENS = int(os.environ.get("AGATE_DEFAULT_MAX_TOKENS", "1024"))

_ddb = boto3.resource("dynamodb")
_sts = boto3.client("sts")


class ChokepointError(Exception):
    """Reject the request (4xx). Never falls through to an unmetered call.

    `code` is a stable machine-readable classifier in the 402 body so a caller (e.g. quarry,
    agate#265) can distinguish a real budget breach from an auth/scope/input error without
    string-matching `detail`. Only `budget_exceeded` means "priced out — degrade gracefully"; the
    rest are hard faults."""

    def __init__(self, detail: str, code: str = "bad_request"):
        super().__init__(detail)
        self.code = code


# Conservative per-image input-token charge (#244). We have no image tokenizer server-side, so we
# over-count in the fail-closed direction: a vision model tokenizes an image up to ~1600 tokens
# (Claude's per-image ceiling). Charged per image so the pre-call gate can't be under-run by
# attaching figures (the chokepoint's exact-spend guarantee — its whole reason to exist).
IMAGE_TOKEN_CHARGE = 1600


def estimate_input_tokens(messages: list[dict]) -> int:
    """Conservative server-side input token estimate (char/4 + a per-image charge, round up). Never
    trusts a client-supplied count — a small lie would shrink the pre-call gate. Counts the raw
    `images` list length BEFORE PNG validation, so it over-counts rather than under."""
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    images = sum(len(m["images"]) for m in messages if isinstance(m.get("images"), list))
    return int(math.ceil(chars / 4)) + 1 + images * IMAGE_TOKEN_CHARGE


def is_auto(model: str | None) -> bool:
    """Whether the request defers model choice to the server-side router (#190).
    A missing model or the literal "auto" means "you pick, within my entitlement"."""
    return not model or model.strip().lower() == "auto"


def _remaining_budget(nodes: list[tuple[str, float, float | None]]) -> float | None:
    """The tightest remaining headroom across the cascade nodes (min of budget-spend),
    for the router's affordability filter. None when NO node sets a budget (unbounded).
    A node with no budget imposes no cap, so it's skipped; a node already over budget
    yields 0 (the router then degrades to the cheapest entitled model and the cascade
    gate makes the real reject decision)."""
    remaining: float | None = None
    for _label, spend, budget in nodes:
        if budget is None:
            continue
        headroom = max(0.0, budget - spend)
        remaining = headroom if remaining is None else min(remaining, headroom)
    return remaining


def to_converse_messages(messages: list[dict]) -> list[dict]:
    """Map the request's chat messages to the Bedrock Converse `messages` shape.

    Converse has no `role:"system"` turn (the system prompt is a separate field), and
    several models — including the default oss tier — reject system messages entirely
    ("This model doesn't support system messages"). The SPA's RAG path prepends grounding
    context as a system message, so fold any system text into the FIRST user turn instead.
    That works for every model and keeps the grounding in front of the question. Pure."""
    system_text = "\n\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    ).strip()
    turns = [m for m in messages if m.get("role") != "system"]
    out: list[dict] = []
    folded = False
    for m in turns:
        content = str(m.get("content", ""))
        if not folded and system_text and m.get("role") == "user":
            content = f"{system_text}\n\n{content}"
            folded = True
        blocks: list[dict] = [{"text": content}]
        # Inline figures (Canvas result→prompt loop, #244): a prompt referencing a code cell's
        # plot passes PNG data-URIs; forward them as Converse image blocks so a vision model can
        # see them. A non-PNG / malformed value is skipped (never sent as an arbitrary blob).
        for block in _image_blocks(m.get("images")):
            blocks.append(block)
        out.append({"role": m["role"], "content": blocks})
    # No user turn to fold into (system-only request) — send the system text as a user turn.
    if system_text and not folded:
        out.insert(0, {"role": "user", "content": [{"text": system_text}]})
    return out


# PNG magic bytes + a decoded-size ceiling (~3.75 MB, Bedrock's per-image limit) and a per-request
# image cap. A saved notebook is an untrusted source of these strings (open reloads cell.output),
# so validate the decoded bytes, not just the data-URI prefix — fail closed (skip), never forward.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MAX_IMAGE_BYTES = 3_750_000
_MAX_IMAGES = 16


def _strip_images(messages: list[dict]) -> list[dict]:
    """Drop `images` and any "[figure from cN]" placeholder lines from the messages — used when the
    resolved model is text-only, so it's never told about a figure it can't see. Pure."""
    import re as _re

    out: list[dict] = []
    for m in messages:
        content = _re.sub(r"\[figure from [^\]]+\]\n?", "", str(m.get("content", "")))
        out.append({"role": m.get("role"), "content": content})
    return out


def _image_blocks(images: object) -> list[dict]:
    """Converse image content blocks from a list of `data:image/png;base64,...` URIs. Skips any
    value that isn't a real base64 PNG within the size limit (defence in depth — a saved notebook
    is untrusted; never forward an arbitrary blob). Caps the count per request."""
    if not isinstance(images, list):
        return []
    import base64 as _b64

    prefix = "data:image/png;base64,"
    blocks: list[dict] = []
    for img in images:
        if len(blocks) >= _MAX_IMAGES:
            break
        if not isinstance(img, str) or not img.startswith(prefix):
            continue
        try:
            raw = _b64.b64decode(img[len(prefix) :], validate=True)
        except Exception:  # noqa: BLE001 — a bad data-URI is skipped, not fatal
            continue
        if not raw.startswith(_PNG_MAGIC) or len(raw) > _MAX_IMAGE_BYTES:
            continue  # not actually a PNG, or too large
        blocks.append({"image": {"format": "png", "source": {"bytes": raw}}})
    return blocks


def lookup_budget(tenant: str, user: str, period: str) -> float | None:
    """The authoritative budget for the verified identity, from the budget table.

    Returns None when no budget table / no row is configured (soft-cap institutions
    don't set Tier 1 caps). The caller cannot influence this — it is keyed by the
    server-derived tenant/user, not by any request field.
    """
    if not BUDGET_TABLE:
        return None
    item = _ddb.Table(BUDGET_TABLE).get_item(Key={"pk": f"{tenant}#{user}#{period}"}).get("Item")
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


def lookup_scope_budget(tenant: str, node: str, period: str) -> float | None:
    """Budget for one scope node (#81 cascade), from the budget table by `scope_pk`.
    None when unconfigured -> that node imposes no cap (evaluate_cascade skips it)."""
    if not BUDGET_TABLE:
        return None
    item = _ddb.Table(BUDGET_TABLE).get_item(Key={"pk": scope_pk(tenant, node, period)}).get("Item")
    return float(item["budget_usd"]) if item and "budget_usd" in item else None


def read_scope_spend(tenant: str, node: str, period: str) -> float:
    """Running spend at one scope node — written by THIS choke point on allow (below).
    The async log meter does not write scope rows, so these are the only writers."""
    if not SPEND_TABLE:
        return 0.0
    return read_scope_spend_item(_ddb.Table(SPEND_TABLE), tenant, node, period)


def _increment_scope_spend(tenant: str, node: str, period: str, cost: float) -> None:
    """Atomically add `cost` USD to a scope-node spend row (created on first write).
    Mirrors meter.handler._increment exactly (Decimal ADD) so the format can't drift."""
    if not SPEND_TABLE:
        return
    _ddb.Table(SPEND_TABLE).update_item(
        Key={"pk": scope_pk(tenant, node, period)},
        UpdateExpression="ADD spend_usd :a",
        ExpressionAttributeValues={":a": Decimal(str(round(cost, 6)))},
    )


def assume_user_role(tags, user: str) -> Any:
    """Assume the authenticated role narrowed by the VERIFIED agate: session tags
    (the SessionTags object from claims_to_tags), returning a scoped Bedrock client.

    The tags are derived from the validated IdP token, not from request fields, so
    the resulting session has exactly the caller's real entitlement — the choke
    point cannot widen access."""
    resp = _sts.assume_role(
        RoleArn=AUTHENTICATED_ROLE_ARN,
        RoleSessionName=(user or "agate-user")[:64],
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
    """Verify the campus-IdP token (real RS256/JWKS via the shared verifier) and
    return its claims. Same verifier the broker uses — no unsigned-token path
    (SEC-4). OIDC config from env; any failure raises ChokepointError (fail closed)."""
    cfg = config_from_env()
    try:
        return verify_token(token, **cfg)
    except TokenError as exc:
        raise ChokepointError(str(exc), code="token_invalid") from exc


def _period_now() -> str:
    """Current billing period (YYYY-MM). Imported lazily so the module stays
    import-light; the broker stamps the same format on invocation records."""
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m")


def process(req: dict, *, period: str | None = None) -> dict:
    """Derive identity from the IdP token, gate on server-side budget, then (on
    allow) invoke the scoped Converse. Identity/budget are NEVER from the body
    (SEC-1) — the body carries only idp_token + model/messages/max_tokens."""
    # Identity: validate the token and derive the agate: tags the SAME way the broker
    # does. tenant/user/tier/courses come from here, never from request fields.
    claims = validate_idp_token(req.get("idp_token", ""))
    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        raise ChokepointError(f"cannot scope session: {exc}", code="scope_denied") from exc
    tenant = tags.tenant
    user = str(claims.get("sub") or claims.get("subject") or "agate-user")
    period = period or _period_now()

    requested_model = req.get("model")
    messages = req.get("messages") or []
    # A missing model is allowed — it means "auto" (the server routes, #190). Only the
    # messages are strictly required.
    if not messages:
        raise ChokepointError("request missing messages")

    max_tokens = int(req.get("max_tokens", DEFAULT_MAX_TOKENS))
    input_tokens = estimate_input_tokens(messages)  # server-side only
    pricebook = default_pricebook()

    # Budget CASCADE (#81): the call must fit under the user/tenant budget AND under
    # every ancestor scope node's budget. The scope comes from the VERIFIED token
    # (tags.scope, #80) — never a request field. ancestors("") == [] so an unconfined
    # session keeps exactly today's user/tenant-only gate. A node with no budget row
    # imposes no cap (evaluate_cascade skips it). scope rows here are the running
    # totals THIS choke point maintains (the async log meter keys only tenant/user).
    scope_nodes = ancestors(tags.scope)
    user_spend = read_spend(tenant, user, period)
    user_budget = lookup_budget(tenant, user, period)
    nodes: list[tuple[str, float, float | None]] = [
        ("user", user_spend, user_budget),
    ]
    for node in scope_nodes:
        nodes.append(
            (
                f"scope:{node}",
                read_scope_spend(tenant, node, period),
                lookup_scope_budget(tenant, node, period),
            )
        )

    # Auto routing (#122/#190): when the client asks for "auto" (or sends no model), the
    # SERVER picks the model — bounded by the VERIFIED tier and the remaining budget,
    # never a client-supplied tier. A concrete model id is used as-is (the picker only
    # ever offers entitled models; the cascade + IAM are the real gate regardless).
    has_images = any(isinstance(m.get("images"), list) and m["images"] for m in messages)
    model_route: dict | None = None
    if is_auto(requested_model):
        # The chokepoint runs no difficulty classifier, so a compute/code/plot request (or a turn
        # carrying a figure) would otherwise route to the CHEAPEST model and fail (#263) — the
        # tier lists aren't strictly capability-ordered, so bumping "difficulty" is unreliable.
        # Instead, for such a request pick the `best` policy (most capable AFFORDABLE model): the
        # top Claude for a frontier session, gpt-oss-120b for a student. Budget still clamps it, and
        # this never exceeds entitlement. An ordinary question keeps the thrifty (cheapest) default.
        last_user = next(
            (str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), ""
        )
        policy = "best" if needs_capable_model(last_user, has_images=has_images) else "thrifty"
        choice = select_model(
            tier=tags.tier,
            policy=policy,
            remaining_budget_usd=_remaining_budget(nodes),
            input_tokens=input_tokens,
            max_tokens=max_tokens,
            pricebook=pricebook,
        )
        model_id = choice.model_id
        model_route = {"model": model_id, "reason": choice.reason, "degraded": choice.degraded}
        # If the turn carries figures (#244) but auto picked a text-only model, upgrade to a
        # vision-capable entitled model so the plot is actually seen (not silently dropped).
        if has_images and not supports_vision(model_id):
            vision = next(
                (m for m in reversed(models_for_tier(tags.tier)) if supports_vision(m)), None
            )
            if vision:
                model_id = vision
                model_route = {
                    "model": model_id,
                    "reason": "vision required for referenced figure",
                    "degraded": False,
                }
    else:
        model_id = requested_model

    fallback_tier = tier_for_model(model_id)  # price unlisted ids at their tier (#88)
    gate = evaluate_cascade(
        model_id=model_id,
        input_tokens=input_tokens,
        max_tokens=max_tokens,
        nodes=nodes,
        pricebook=pricebook,
        fallback_tier=fallback_tier,
    )
    if gate.decision == "reject":
        raise ChokepointError(
            f"pre-call budget check failed at {gate.breaching_node}: {gate.reason}",
            code="budget_exceeded",
        )

    # If the resolved model is text-only but the turn carries figures, strip the images AND their
    # "[figure from cN]" placeholders — never tell a blind model about a figure it can't see (that
    # invites a hallucinated description). A vision model keeps both.
    send_messages = messages
    if has_images and not supports_vision(model_id):
        send_messages = _strip_images(messages)

    # Allowed: invoke Converse with the role narrowed by the VERIFIED tags.
    br = assume_user_role(tags, user)
    resp = br.converse(
        modelId=model_id,
        messages=to_converse_messages(send_messages),
        inferenceConfig={"maxTokens": max_tokens},
    )
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
    usage = resp.get("usage", {})
    # Record ACTUAL cost against each ancestor scope node (running totals for the
    # cascade). Only scope rows — user/tenant rows stay owned by the async log meter,
    # so there's no double count. Best-effort: a metering write must not fail the
    # already-served call.
    in_tok = int(usage.get("inputTokens", 0))
    out_tok = int(usage.get("outputTokens", 0))
    actual_cost = estimate_call_cost(
        model_id, in_tok, out_tok, pricebook=pricebook, fallback_tier=fallback_tier
    )
    for node in scope_nodes:
        _increment_scope_spend(tenant, node, period, actual_cost)
    # Report the period's spend/budget so the UI can show where the user stands.
    # The async log meter owns the authoritative user/tenant spend rows, so the
    # value read above predates this call; add this call's actual cost for a
    # close (still non-authoritative) running figure. budget is None = no cap set.
    spend_after = user_spend + actual_cost
    result = {
        "text": text,
        "usage": {"inputTokens": in_tok, "outputTokens": out_tok},
        "estimated_cost": gate.estimated_cost,
        "cost": actual_cost,
        # The model that actually ran (so the UI shows it even under "auto"), plus the
        # routing rationale when the server picked it.
        "model": model_id,
        "budget": {
            "period": period,
            "spend_usd": round(spend_after, 6),
            "budget_usd": round(user_budget, 6) if user_budget is not None else None,
        },
    }
    if model_route is not None:
        result["model_route"] = model_route
    return result


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
        # `code` classifies the rejection (agate#265): budget_exceeded | token_invalid |
        # scope_denied | bad_request. Only budget_exceeded means "priced out" — a caller maps the
        # rest to hard faults, not graceful degradation. `error` stays for back-compat.
        return _resp(402, {"error": "budget_rejected", "code": exc.code, "detail": str(exc)})
    except Exception:  # noqa: BLE001 — fail closed
        # Log the traceback (CloudWatch) so a 500 is diagnosable; the response body
        # stays opaque (no internals leaked to the caller). Behaviour unchanged.
        logging.exception("chokepoint_error")
        return _resp(500, {"error": "chokepoint_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
