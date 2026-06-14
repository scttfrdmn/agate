"""run_panel — N-model Panel orchestration (§10.2.4).

Every roster member reviews the SAME evidence independently and in parallel, then
a separate adjudicator reconciles them into the structured `Divergence` (§10.2.5).
Each member streams its own start/done + per-call cost so the SPA renders one live
pane per model (keyed by `pane`). The adjudication tail validates the structured
output and emits a `divergence` event; on malformed output it falls back to an
unstructured `answer` rather than failing the run.

Pure orchestration over injected interfaces — no boto3 here. The deployed agent
(AgentCore Runtime, behind the Strands reference agent) supplies Bedrock-backed
`Backend`/`CostMeter` implementations; tests supply fakes. Reviewer labels are
roster configuration, kept neutral (never product names) by repo convention.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from pydantic import ValidationError

from agate.contracts import Backend, CostMeter, Emit
from agate.panel.schema import Divergence, strip_fences

__all__ = ["Backend", "CostMeter", "run_panel"]


def _monotonic() -> float:
    # Indirection so tests can stay deterministic if they patch it; production uses
    # the real clock for elapsed timing only (never for control flow).
    return time.monotonic()


def run_panel(
    *,
    backend: Backend,
    meter: CostMeter,
    emit: Emit,
    question: str,
    evidence: str,
    roster: list[dict[str, Any]],
    adjudicator: dict[str, Any],
    review_system: str,
    adjudicate_system: str,
) -> dict[str, Any]:
    """Run every roster member over the SAME evidence in parallel, then reconcile.

    roster:      [{"tier", "label", "max_tokens"}, ...] — mixed families/weights.
    adjudicator: {"tier", "label", "max_tokens"}.
    Returns {label: review_text, ..., "__adjudication__": payload}.
    """
    lock = threading.Lock()

    def safe_emit(ev: dict[str, Any]) -> None:
        with lock:
            emit(ev)

    prompt = f"Evidence:\n{evidence}\n\nQuestion: {question}"

    def review(member: dict[str, Any]) -> tuple[str, str]:
        tier, label, max_tok = member["tier"], member["label"], member["max_tokens"]
        safe_emit({"type": "model", "tier": tier, "label": label, "state": "start", "pane": label})
        t0 = _monotonic()
        # A reasoning PATTERN gives each role its own system prompt (the institution's
        # recipe); fall back to the shared review_system when a member has none.
        member_system = member.get("system") or review_system
        text, usage, _ = backend.converse(tier, member_system, prompt, max_tok)
        cost = meter.add_llm(f"panel · {label}", tier, label, usage)
        safe_emit(
            {
                "type": "model",
                "tier": tier,
                "label": label,
                "state": "done",
                "pane": label,
                "elapsed_s": round(_monotonic() - t0, 1),
                "usage": {
                    "inputTokens": usage.get("inputTokens", 0),
                    "outputTokens": usage.get("outputTokens", 0),
                },
                "cost": round(cost, 6),
            }
        )
        safe_emit({"type": "cost", "total": round(meter.total, 6)})
        return label, text

    # Fan out: every member reviews the same evidence independently, in parallel.
    reviews: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(roster))) as pool:
        futures = [pool.submit(review, m) for m in roster]
        for fut in as_completed(futures):
            label, text = fut.result()
            reviews[label] = text

    # Reconcile. The adjudicator must return ONLY JSON conforming to the Divergence
    # schema. Parse and validate defensively; emit `divergence` + a short `answer`.
    transcript = "\n\n".join(f"REVIEW — {label}:\n{txt}" for label, txt in reviews.items())
    raw, usage, _ = backend.converse(
        adjudicator["tier"], adjudicate_system, transcript, adjudicator["max_tokens"]
    )
    meter.add_llm("panel · adjudication", adjudicator["tier"], adjudicator["label"], usage)
    safe_emit({"type": "cost", "total": round(meter.total, 6)})

    try:
        payload = json.loads(strip_fences(raw))
        Divergence.model_validate(payload)  # contract guard (§10.2.5)
        safe_emit({"type": "divergence", **payload})
        if payload.get("summary"):
            safe_emit({"type": "answer", "title": "Panel — reconciled", "text": payload["summary"]})
    except (json.JSONDecodeError, ValidationError):
        # Adjudicator broke the contract: surface raw text rather than failing.
        safe_emit(
            {
                "type": "answer",
                "title": "Panel — reconciled (unstructured)",
                "text": raw,
            }
        )
        payload = {"summary": raw, "claims": []}

    reviews["__adjudication__"] = payload
    return reviews
