"""AgentCore Runtime container entrypoint (design §13.7).

A minimal HTTP server honouring the AgentCore Runtime invocation protocol:
  POST /invocations  -> receive the payload blob, run dispatch, return the event
                        stream as newline-delimited JSON (one RunEvent per line)
  GET  /ping         -> health check

Standard-library only (no web framework — CLAUDE.md "no OSS middleware in core").
The Runtime invokes this; the per-session microVM scales to zero between calls, so
there is no idle clock. The orchestration is the pure `agate.agent_dispatch.dispatch`
driven by the Bedrock-backed adapters in `agent.backends`.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agate.agent_dispatch import InvocationError, dispatch
from agate.entitlements import DEFAULT_TIER, models_for_tier
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.patterns import PatternError, compile_pattern
from agate.patterns import get as pattern_get
from agate.tags import ClaimsError, claims_to_tags
from cost import CostMeter

from agent import memory_client
from agent.backends import (
    BedrockBackend,
    CodeInterpreterRunner,
    decode_payload,
)

REGION = os.environ.get("AGATE_REGION", "us-east-1")
CODE_INTERPRETER_ID = os.environ.get("AGATE_CODE_INTERPRETER_ID", "")
PORT = int(os.environ.get("PORT", "8080"))
# AgentCore passes the multi-turn session id as this header; the memory hook keys the
# session tier on it. Lower-cased (http.server normalises header lookups case-insensitively).
_SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"


def _verified_tier(payload: dict) -> str:
    """Derive the caller's tier from the VERIFIED IdP token in the payload (SEC-4b).

    The tier is NOT taken from a request header or a payload field — it is the
    `agate:tier` derived from the token's claims after real RS256/JWKS verification
    (shared agate.jwt_verify), the same path the broker and choke point use. Any
    verification failure or missing token falls back to the cheapest tier (oss) —
    fail closed, never unrestricted.
    """
    cfg = config_from_env()
    try:
        claims = verify_token(payload.get("idp_token", ""), **cfg)
        return claims_to_tags(claims).tier
    except (TokenError, ClaimsError):
        return DEFAULT_TIER


def _spend_metadata(payload: dict) -> dict[str, str]:
    """The tenant/user attribution to attach to Bedrock calls (#77).

    Derived from the VERIFIED token — same path as the tier. Surfaces in the Bedrock
    invocation log's requestMetadata, where the authoritative-spend meter reads
    `agate:tenant` to attribute spend per tenant/user. Empty on an unverifiable token
    (meter then keys 'unknown'); it's an attribution hint, never a security boundary.
    """
    cfg = config_from_env()
    try:
        claims = verify_token(payload.get("idp_token", ""), **cfg)
        tags = claims_to_tags(claims)
    except (TokenError, ClaimsError):
        return {}
    user = str(claims.get("sub") or "unknown")
    return {"agate:tenant": tags.tenant, "agate:user": user}


def _resolve_models(payload: dict, models: list[str]) -> dict:
    """Fill missing roster/generator/router model ids with concrete, entitled ones.

    `agate.agent_dispatch` treats a config's `tier` field as the literal Bedrock
    model id at the Backend boundary (the roster is meant to be pinned to real ids
    at config time). The SPA, however, sends only `{question, idp_token, mode}` and
    no roster — so here, in the container, we materialise sensible defaults from the
    caller's ENTITLED model set (derived from the verified token). This keeps the
    pure dispatch contract intact (label==id) while never sending a bare logical
    label like "oss" as a modelId (which Bedrock rejects as invalid).

    Cheapest entitled model drives the router (a 1-word classification) and the Ask
    generator; a small distinct panel is built for DEBATE when none was supplied.
    Anything the payload DID specify is left untouched.
    """
    if not models:
        return payload
    cheapest = models[0]
    p = dict(payload)
    p.setdefault("router", {"tier": cheapest, "label": "router", "max_tokens": 5})
    p.setdefault("generator", {"tier": cheapest, "label": "ask", "max_tokens": 1024})
    # DEBATE needs a roster + adjudicator; build one from up to 3 distinct entitled
    # models (falling back to repeats if the tier has fewer) when the SPA sent none.
    if (payload.get("mode") == "DEBATE" or payload.get("router")) and not payload.get("roster"):
        picks = (models + models + models)[:3]
        p["roster"] = [
            {"tier": m, "label": f"model-{i + 1}", "max_tokens": 1024} for i, m in enumerate(picks)
        ]
        p.setdefault("adjudicator", {"tier": cheapest, "label": "adjudicator", "max_tokens": 1024})
    return p


def run_invocation(payload: dict, *, session_id: str = "") -> list[dict]:
    """Run one invocation and return the ordered event stream.

    Per-invocation backends so a microVM serving one session holds no cross-session
    state. Emits a terminal `receipt` event built from the meter's rows.

    SEC-2/SEC-4b: the entitled-model set comes from the tier derived from the
    VERIFIED inbound token (`_verified_tier`), never from a payload field or an
    unsourced header. `dispatch` rejects any payload-named model outside that set
    before invoking; an unverifiable token fails closed to oss.

    Memory (#130b): when a memory tool is wired (opt-in), recall personal memory and
    prepend it to the invocation `evidence` BEFORE dispatch, and record the turn AFTER.
    Both are best-effort via `agent.memory_client` (the memory boundary re-verifies the
    forwarded token + fences the namespace server-side) — a memory failure never breaks
    the turn, and with no tool wired both are silent no-ops.
    """
    events: list[dict] = []
    emit = events.append

    idp_token = payload.get("idp_token", "")
    # Recall BEFORE dispatch: fold remembered context into the evidence the reasoning
    # modes (DEBATE/Ask) already consume. Best-effort; [] when disabled or on failure.
    if memory_client.enabled() and idp_token:
        recalled = memory_client.recall(
            idp_token,
            tier="personal",
            query=(payload.get("question") or "").strip(),
            session_id=session_id,
        )
        block = memory_client.recall_as_evidence(recalled)
        if block:
            existing = payload.get("evidence", "")
            payload = {**payload, "evidence": f"{block}\n\n{existing}".strip()}

    # Attach tenant/user attribution so the Bedrock invocation log can be metered
    # per tenant (#77); derived from the verified token, sanitised in the backend.
    backend = BedrockBackend(REGION, request_metadata=_spend_metadata(payload))
    # Authoritative dollar metering (cost.CostMeter); a roster-supplied PriceBook
    # could be threaded in per invocation, but the hard-default rates keep the
    # receipt coherent out of the box.
    meter = CostMeter()
    runner = CodeInterpreterRunner(REGION, CODE_INTERPRETER_ID) if CODE_INTERPRETER_ID else None

    tier = _verified_tier(payload)
    entitled = models_for_tier(tier)
    allowed_models = set(entitled)
    # A reasoning PATTERN (Phase 9 Track 2): when the payload names a registered
    # pattern, compile it against the caller's ENTITLED models into a dispatch payload
    # (roster + per-role prompts + adjudicator). This composes the existing primitives
    # — dispatch runs it unchanged. An unknown pattern key surfaces as an error event.
    if payload.get("pattern"):
        try:
            pat = pattern_get(payload["pattern"])
            payload = {
                **compile_pattern(
                    pat,
                    question=(payload.get("question") or "").strip(),
                    entitled_models=entitled,
                    evidence=payload.get("evidence", ""),
                ),
                # carry the verified token through for any downstream checks
                "idp_token": payload.get("idp_token", ""),
            }
        except PatternError as exc:
            emit({"type": "answer", "title": "error", "text": f"pattern: {exc}"})
            emit(meter.receipt().to_event())
            return events
    # Materialise concrete entitled model ids for any config the caller omitted, so
    # a bare {question, idp_token, mode} from the SPA runs without an invalid modelId.
    payload = _resolve_models(payload, entitled)

    try:
        dispatch(
            payload,
            backend=backend,
            meter=meter,
            emit=emit,
            code_runner=runner,
            allowed_models=allowed_models,
        )
    except InvocationError as exc:
        emit({"type": "answer", "title": "error", "text": str(exc)})

    # Record the turn AFTER dispatch (best-effort, opt-in). The answer events become the
    # session memory the next turn recalls; the memory tool re-verifies the token + fences
    # the namespace server-side, so the container forwards only the verified token.
    if memory_client.enabled() and idp_token and session_id:
        answers = [e for e in events if e.get("type") == "answer" and e.get("title") != "error"]
        if answers:
            memory_client.record(idp_token, answers, session_id=session_id)

    # Close the run with an itemised receipt (the meter's rows + total).
    emit(meter.receipt().to_event())
    return events


def _events_to_blob(events: list[dict]) -> bytes:
    """Newline-delimited JSON — one event per line (a streamable transcript)."""
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — http.server API
        if self.path == "/ping":
            self._send(200, b'{"status":"healthy"}')
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        if self.path != "/invocations":
            self._send(404, b'{"error":"not found"}')
            return
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            payload = decode_payload(body)
            # The AgentCore multi-turn session id arrives as a header; fall back to a
            # payload field. It only keys the memory session tier — identity/tenant/scope
            # are still derived from the verified token, never this value.
            session_id = self.headers.get(_SESSION_HEADER) or payload.get("session_id") or ""
            # SEC-4b: the tier is derived from the VERIFIED IdP token inside
            # run_invocation — not from a request header (which had no trusted
            # source). The SPA includes idp_token in the invocation payload.
            events = run_invocation(payload, session_id=session_id)
            self._send(200, _events_to_blob(events), content_type="application/x-ndjson")
        except Exception as exc:  # noqa: BLE001 — never 500 silently
            self._send(500, json.dumps({"error": "agent_error", "detail": str(exc)}).encode())

    def log_message(self, *_args) -> None:  # quiet default logging
        pass

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
