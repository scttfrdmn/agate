"""AgentCore Runtime container entrypoint (design §13.7).

A minimal HTTP server honouring the AgentCore Runtime invocation protocol:
  POST /invocations  -> receive the payload blob, run dispatch, return the event
                        stream as newline-delimited JSON (one RunEvent per line)
  GET  /ping         -> health check

Standard-library only (no web framework — CLAUDE.md "no OSS middleware in core").
The Runtime invokes this; the per-session microVM scales to zero between calls, so
there is no idle clock. The orchestration is the pure `agg.agent_dispatch.dispatch`
driven by the Bedrock-backed adapters in `agent.backends`.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agg.agent_dispatch import InvocationError, dispatch
from agg.entitlements import DEFAULT_TIER, TIERS, models_for_tier
from cost import CostMeter

from agent.backends import (
    BedrockBackend,
    CodeInterpreterRunner,
    decode_payload,
)

REGION = os.environ.get("AGG_REGION", "us-east-1")
CODE_INTERPRETER_ID = os.environ.get("AGG_CODE_INTERPRETER_ID", "")
PORT = int(os.environ.get("PORT", "8080"))


def run_invocation(payload: dict, *, verified_tier: str | None = None) -> list[dict]:
    """Run one invocation and return the ordered event stream.

    Per-invocation backends so a microVM serving one session holds no cross-session
    state. Emits a terminal `receipt` event built from the meter's rows.

    SEC-2: `verified_tier` is the agg:tier from the VALIDATED inbound JWT (AgentCore
    Identity / Cognito authorizer), NOT from the payload. We expand it to the set of
    entitled model ids and pass it to dispatch, which rejects any payload-named model
    outside that set before invoking. An unknown/missing verified tier falls back to
    the cheapest tier (oss) — fail closed, never unrestricted.
    """
    events: list[dict] = []
    emit = events.append

    backend = BedrockBackend(REGION)
    # Authoritative dollar metering (cost.CostMeter); a roster-supplied PriceBook
    # could be threaded in per invocation, but the hard-default rates keep the
    # receipt coherent out of the box.
    meter = CostMeter()
    runner = CodeInterpreterRunner(REGION, CODE_INTERPRETER_ID) if CODE_INTERPRETER_ID else None

    tier = verified_tier if verified_tier in TIERS else DEFAULT_TIER
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
            # SEC-2: the verified agg:tier comes from the inbound identity the
            # AgentCore custom_jwt_authorizer validated and propagates as a header —
            # NOT from the payload. Absent -> run_invocation falls back to the
            # cheapest tier (fail closed).
            verified_tier = self.headers.get("X-Agg-Verified-Tier")
            events = run_invocation(payload, verified_tier=verified_tier)
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
