"""Natural-language drafting endpoint (#118b / #118, vision §8.5).

The live surface behind the disposer: a user types *"an agent that summarizes new papers in
my lab every Monday"* → this endpoint asks the author's OWN entitled model to draft a spec →
`agate.drafting.dispose_draft` CLAMPS it to what the author verifiably holds → returns the
bounded plan for human confirmation. Nothing compiles to a live agent here.

The load-bearing thesis — **the LLM proposes, the compiler disposes**: the model's output is
a string of JSON with ZERO authority. It becomes a plan only by passing the existing
fail-closed pipeline (`parse_spec` → `delegate` clamp → `describe_instantiated`), where
authority originates ONLY from the verified author tags, never the draft. So the model call
needs no per-tenant data fence — it can't widen anything. The Lambda invokes Bedrock under its
own role (scoped to the entitled-model superset in IAM); the per-SESSION tier is enforced HERE
in code by drafting with `models_for_tier(verified_tier)[0]` — the agent-runtime discipline.

Per-request Lambda behind an IAM-authed Function URL, NO CLOCKS. Fails closed: a
verification/scoping failure returns an error envelope; a bad model output is a clean
`ok=False` draft outcome, never a 500.
"""

from __future__ import annotations

import json
import os
import re

import boto3
from agate.drafting import dispose_draft, draft_system_prompt
from agate.entitlements import DEFAULT_TIER, models_for_tier
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.tags import ClaimsError, claims_to_tags

REGION = os.environ.get("AGATE_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
# A draft is a cheap one-shot; cap it so a draft can't run away. Non-secret deploy config.
DRAFT_MAX_TOKENS = int(os.environ.get("AGATE_DRAFT_MAX_TOKENS", "1024"))

_bedrock = boto3.client("bedrock-runtime", region_name=REGION)

# Bedrock requestMetadata values must match [a-zA-Z0-9\s:_@$#=/+,.-]{1,256}; sanitise.
_META_RE = re.compile(r"[^a-zA-Z0-9\s:_@$#=/+,.-]")


class DraftingError(ValueError):
    """A drafting request that cannot be served safely. Fail closed."""


def validate_idp_token(token: str) -> dict:
    """Verify the campus-IdP token (real RS256/JWKS) — the SAME verifier the broker, retrieval
    proxy, and slurm server use. The inbound identity is the verified author."""
    if not token or not isinstance(token, str):
        raise DraftingError("missing idp_token")
    try:
        return verify_token(token, **config_from_env())
    except TokenError as exc:
        raise DraftingError(f"token verification failed: {exc}") from exc


def _extract_json(text: str) -> dict | None:
    """Parse the model's drafted spec from its text. Strips a leading ```/```json fence and a
    trailing ``` (the prompt forbids fences, but models disobey), then `json.loads`. Returns
    None on any parse failure — the caller turns that into a clean `ok=False` draft outcome
    (a bad draft is a user-facing 'try rephrasing', NOT a server error)."""
    t = (text or "").strip()
    if t.startswith("```"):
        # drop the opening fence line (``` or ```json), keep the rest
        nl = t.find("\n")
        t = t[nl + 1 :] if nl != -1 else ""
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    t = t.strip()
    if not t:
        return None
    try:
        out = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


def _draft_spec(request: str, tags, *, subject: str) -> str:
    """Ask the author's cheapest entitled model to draft a spec JSON. The model is bounded to
    the entitled set by IAM; the per-session tier is enforced HERE — we draft with a model
    from `models_for_tier(verified_tier)`, so an oss author never drafts with a frontier model.
    `requestMetadata` carries tenant/user for spend attribution (#77)."""
    models = models_for_tier(tags.tier or DEFAULT_TIER)
    model_id = models[0]  # cheapest entitled; a draft is a one-shot
    meta = {
        k: _META_RE.sub("-", str(v))[:256]
        for k, v in {"agate:tenant": tags.tenant, "agate:user": subject}.items()
        if v
    }
    kwargs = {
        "modelId": model_id,
        "system": [{"text": draft_system_prompt(tags)}],
        "messages": [{"role": "user", "content": [{"text": request}]}],
        "inferenceConfig": {"maxTokens": DRAFT_MAX_TOKENS},
    }
    if meta:
        kwargs["requestMetadata"] = meta
    resp = _bedrock.converse(**kwargs)
    return "".join(b.get("text", "") for b in resp["output"]["message"]["content"])


def process(req: dict) -> dict:
    """Draft → dispose → render. `req` carries the verified `idp_token` and the user's
    natural-language `request`. Identity/tier are derived from the token; the model output is
    clamped to the author's authority by `dispose_draft`. Returns the bounded plan to confirm,
    or a fail-closed `ok=False` reason — nothing compiles."""
    claims = validate_idp_token(req.get("idp_token", ""))
    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        raise DraftingError(f"cannot scope session: {exc}") from exc
    subject = str(claims.get("sub") or claims.get("subject") or "agate-user")

    request = str(req.get("request") or "").strip()
    if not request:
        raise DraftingError("missing request")

    text = _draft_spec(request, tags, subject=subject)
    draft = _extract_json(text)
    if draft is None:
        # The model didn't emit usable JSON — a draft outcome, not a server error.
        return {"ok": False, "reason": "the model did not emit a valid spec; try rephrasing"}

    outcome = dispose_draft(draft, tags, subject=subject)
    # Return the legible plan + the validated spec to confirm — never the InstantiatedAgent or
    # credential. On confirm the SPA echoes `spec` to the deploy endpoint, which RE-RUNS
    # dispose_draft against the verified token (re-clamping server-side) before persisting, so
    # the echoed spec is a convenience, NOT a trusted authority input — a tampered spec is
    # re-clamped or rejected exactly as a fresh draft would be (#118 deploy-on-confirm).
    resp = {"ok": outcome.ok, "reason": outcome.reason, "plan": outcome.summary()}
    if outcome.ok:
        resp["spec"] = draft  # the validated draft dict (parse_spec accepted it inside dispose)
    return resp


def handler(event: dict, context: object) -> dict:
    """Function URL entry point. Fail-closed: a verification/scoping failure returns an error
    envelope, never a silent broad action."""
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        req = json.loads(body) if isinstance(body, str) else body
        return _resp(200, process(req))
    except DraftingError as exc:
        return _resp(403, {"error": "not_entitled", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        return _resp(500, {"error": "drafting_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
