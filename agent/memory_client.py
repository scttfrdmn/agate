"""Runtime → Memory bridge (#130b): the container's per-turn recall/record hook.

The agent Runtime is a per-session microVM running under ONE shared execution role (not
tenant-tagged), so it must NOT call AgentCore Memory directly — that would hit the exact
inert-fence problem #130 fixed (a namespacePath policy needs the caller to CARRY the
`agate:` tags). Instead the container invokes the already-reviewed memory tool Lambda,
forwarding the verified `idp_token` it received. The Lambda re-verifies identity at the
memory boundary, assumes the tenant-fenced role, and derives the namespace from
`namespaces_for` — so the security boundary is enforced ONCE, server-side, and the
container trusts nothing of its own about identity.

OPT-IN / fail-open-to-no-memory: when `AGATE_MEMORY_TOOL_ARN` is unset (the default — the
billable memory stack isn't deployed), every call here is a silent no-op. Memory is an
enhancement to a turn, never a gate: a recall/record failure must NEVER break the
invocation, so all errors are swallowed (the turn proceeds without memory).
"""

from __future__ import annotations

import json
import os

import boto3

REGION = os.environ.get("AGATE_REGION", "us-east-1")
MEMORY_TOOL_ARN = os.environ.get("AGATE_MEMORY_TOOL_ARN", "")

# Built lazily so the import (and the whole container) works with no memory configured.
_lambda = None


def _client():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda", region_name=REGION)
    return _lambda


def enabled() -> bool:
    """True only when a memory tool is wired (the opt-in billable stack is deployed)."""
    return bool(MEMORY_TOOL_ARN)


def _invoke(req: dict) -> dict | None:
    """Invoke the memory tool Lambda with one request envelope; return its parsed body or
    None on any failure. Never raises — memory is best-effort, never a gate."""
    if not enabled():
        return None
    try:
        resp = _client().invoke(
            FunctionName=MEMORY_TOOL_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps({"body": json.dumps(req)}).encode("utf-8"),
        )
        out = json.loads(resp["Payload"].read() or b"{}")
        if out.get("statusCode") != 200:
            return None
        return json.loads(out.get("body") or "{}")
    except Exception:  # noqa: BLE001 — best-effort; a memory failure never breaks the turn
        return None


def recall(idp_token: str, *, tier: str = "personal", query: str = "", session_id: str = "",
           max_results: int = 5) -> list[dict]:
    """Recall records from one memory tier for the verified caller. Returns [] when memory
    is disabled or on any failure. Identity/namespace are resolved server-side by the tool —
    the container passes only the verified token + the tier it wants."""
    body = _invoke({
        "idp_token": idp_token,
        "op": "recall",
        "tier": tier,
        "query": query,
        "session_id": session_id,
        "max_results": max_results,
    })
    if not body:
        return []
    records = body.get("records")
    return records if isinstance(records, list) else []


def record(idp_token: str, payload: list[dict], *, session_id: str) -> bool:
    """Record one turn's events to the caller's session memory. Returns True on success,
    False when disabled or on any failure (best-effort — never breaks the turn)."""
    if not payload or not session_id:
        return False
    body = _invoke({
        "idp_token": idp_token,
        "op": "record",
        "session_id": session_id,
        "payload": payload,
    })
    return bool(body and body.get("recorded"))


def recall_as_evidence(records: list[dict]) -> str:
    """Render recalled memory records into a text block to PREPEND to the invocation's
    `evidence` (which DEBATE + Ask already consume). Pure; safe on []."""
    lines = []
    for r in records:
        text = r.get("content") or r.get("text") or r.get("memoryContent") or ""
        if isinstance(text, dict):
            text = text.get("text", "")
        text = str(text).strip()
        if text:
            lines.append(f"- {text}")
    if not lines:
        return ""
    return "Relevant remembered context:\n" + "\n".join(lines)
