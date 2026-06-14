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
from agate.tags import ClaimsError, claims_to_tags
from cost import CostMeter

from agent.backends import (
    BedrockBackend,
    CodeInterpreterRunner,
    decode_payload,
)

REGION = os.environ.get("AGATE_REGION", "us-east-1")
CODE_INTERPRETER_ID = os.environ.get("AGATE_CODE_INTERPRETER_ID", "")
PORT = int(os.environ.get("PORT", "8080"))


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


def run_invocation(payload: dict) -> list[dict]:
    """Run one invocation and return the ordered event stream.

    Per-invocation backends so a microVM serving one session holds no cross-session
    state. Emits a terminal `receipt` event built from the meter's rows.

    SEC-2/SEC-4b: the entitled-model set comes from the tier derived from the
    VERIFIED inbound token (`_verified_tier`), never from a payload field or an
    unsourced header. `dispatch` rejects any payload-named model outside that set
    before invoking; an unverifiable token fails closed to oss.
    """
    events: list[dict] = []
    emit = events.append

    backend = BedrockBackend(REGION)
    # Authoritative dollar metering (cost.CostMeter); a roster-supplied PriceBook
    # could be threaded in per invocation, but the hard-default rates keep the
    # receipt coherent out of the box.
    meter = CostMeter()
    runner = CodeInterpreterRunner(REGION, CODE_INTERPRETER_ID) if CODE_INTERPRETER_ID else None

    tier = _verified_tier(payload)
    allowed_models = set(models_for_tier(tier))

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
            # SEC-4b: the tier is derived from the VERIFIED IdP token inside
            # run_invocation — not from a request header (which had no trusted
            # source). The SPA includes idp_token in the invocation payload.
            events = run_invocation(payload)
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
