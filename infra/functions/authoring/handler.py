"""Graphical authoring endpoint (#117, vision §8.5) — the bounded-menu surface.

Serves the visual builder + template gallery. Two ops, both PURE-clamp (no model call, unlike
the #118b drafting endpoint — graphical authoring picks from a menu, it doesn't generate):

  * `options` (read): the bounded menu (`authoring_options`) the builder renders — only tiers
    ≤ the author's and scope nodes the author CONTAINS, plus the capability/skill/pattern
    catalogs + the template gallery. Unsafe is unrepresentable: the menu literally cannot
    offer an over-broad scope/tier (escalation = the absence of the button).
  * `dispose` (write-intent): funnel a builder-assembled (or template-filled) spec dict
    through the SAME disposer an LLM draft uses (`author_from_options` → `dispose_draft`):
    parse → clamp to the author → render the effective boundary to confirm. So even a client
    that bypasses the bounded UI and POSTs a hand-crafted selection is clamped or rejected
    exactly as a hallucinated LLM draft — the COMPILER is the authority, the menu is UX.

Identity (tier/scope/courses) is derived from the VERIFIED token, never the body. The candidate
scope nodes the picker offers are seeded from the author's OWN scope + their verified `courses`
(course-shaped sub-nodes), then containment-filtered by `offerable_scopes` — so the source can
only ever NARROW, never widen. Per-request Lambda behind an IAM-authed Function URL; NO model,
NO write (deploy-on-confirm is the #118 deploy endpoint). Fails closed.
"""

from __future__ import annotations

import json
import os

from agate.authoring import (
    author_from_options,
    authoring_options,
    get_template,
    template_gallery,
)
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.tags import ClaimsError, SessionTags, claims_to_tags

REGION = os.environ.get("AGATE_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


class AuthoringError(ValueError):
    """An authoring request that cannot be served safely. Fail closed."""


def validate_idp_token(token: str) -> dict:
    """Verify the campus-IdP token (real RS256/JWKS) — the SAME verifier the broker, retrieval
    proxy, drafting, and deploy use. The inbound identity is the verified author."""
    if not token or not isinstance(token, str):
        raise AuthoringError("missing idp_token")
    try:
        return verify_token(token, **config_from_env())
    except TokenError as exc:
        raise AuthoringError(f"token verification failed: {exc}") from exc


def _identity(req: dict) -> tuple[SessionTags, str]:
    claims = validate_idp_token(req.get("idp_token", ""))
    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        raise AuthoringError(f"cannot scope session: {exc}") from exc
    subject = str(claims.get("sub") or claims.get("subject") or "agate-user")
    return tags, subject


def _candidate_scope_nodes(tags: SessionTags) -> tuple[str, ...]:
    """Seed the scope picker's candidates from the VERIFIED session: the author's own scope +
    their enrolled courses as sub-nodes under that scope (a course `chem-101` under scope
    `chemistry` → candidate `chemistry/chem-101`; under an unscoped/tenant-wide author → the
    bare course id). `offerable_scopes` then containment-filters this list, so a candidate the
    author doesn't actually contain is dropped — the seed can only narrow, never widen."""
    base = tags.scope.strip("/")
    nodes: list[str] = []
    for course in tags.courses:
        c = str(course).strip("/")
        if not c:
            continue
        nodes.append(f"{base}/{c}" if base else c)
    return tuple(nodes)


def options(req: dict) -> dict:
    """The bounded menu the builder renders + the template gallery. Everything selectable is
    pre-clamped to the author's reach (unsafe is unrepresentable)."""
    tags, _ = _identity(req)
    opts = authoring_options(tags, _candidate_scope_nodes(tags))
    return {"ok": True, "options": opts.to_dict(), "templates": template_gallery()}


def dispose(req: dict) -> dict:
    """Dispose a builder-assembled (or template-filled) spec against the VERIFIED author. The
    spec is the builder's form state (or `get_template` + the author's filled slots); it
    carries NO authority — `author_from_options` clamps it to the author exactly as an LLM
    draft. Returns the bounded plan + the validated spec to confirm (the SPA echoes `spec` to
    the #118 deploy endpoint, which re-clamps server-side)."""
    tags, subject = _identity(req)
    spec = req.get("spec")
    # A template id is a convenience: fetch its skeleton, then overlay the author's slots.
    template_id = req.get("template")
    if template_id is not None:
        skeleton = get_template(str(template_id))
        if skeleton is None:
            raise AuthoringError(f"unknown template: {template_id!r}")
        overlay = spec if isinstance(spec, dict) else {}
        spec = {**skeleton, **overlay}
    if not isinstance(spec, dict) or not spec:
        raise AuthoringError("missing spec")

    outcome = author_from_options(spec, tags, subject=subject)
    resp = {"ok": outcome.ok, "reason": outcome.reason, "plan": outcome.summary()}
    if outcome.ok:
        # The validated spec the SPA echoes to deploy-on-confirm (#118) — re-clamped there.
        resp["spec"] = spec
    return resp


def process(req: dict) -> dict:
    """Route one authoring call on `op` (`options`|`dispose`)."""
    op = req.get("op", "options")
    if op == "options":
        return options(req)
    if op == "dispose":
        return dispose(req)
    raise AuthoringError(f"unknown op: {op!r}")


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
    except AuthoringError as exc:
        return _resp(403, {"error": "not_entitled", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        return _resp(500, {"error": "authoring_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
