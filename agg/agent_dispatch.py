"""Pure agent dispatch (design §13.7) — route an invocation to an orchestration.

This is the brain of the reference agent that runs on AgentCore Runtime: given a
decoded invocation payload, it resolves the interaction mode (router or explicit
override) and drives the matching orchestration — Ask, Panel (`run_panel`), or
Analyze (`run_analyze`) — emitting the run event stream the SPA renders.

Pure and AWS-free: it composes the already-tested `agg.router` / `agg.panel` /
`agg.analyze` over injected `Backend` / `CodeRunner` / `CostMeter`. The container
(`agent/`) supplies Bedrock-backed implementations; tests supply fakes. Keeping
dispatch here means it is covered by the same no-AWS unit suite as the rest of agg.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agg.analyze import ANALYZE_SYSTEM, run_analyze
from agg.panel import ADJUDICATE_SYSTEM, run_panel
from agg.panel.prompts import REVIEW_SYSTEM
from agg.router import run_router

Emit = Callable[[dict[str, Any]], None]


class InvocationError(ValueError):
    """The payload is missing what a mode needs — surfaced as an error event."""


def dispatch(
    payload: dict[str, Any],
    *,
    backend: Any,
    meter: Any,
    emit: Emit,
    code_runner: Any | None = None,
) -> dict[str, Any]:
    """Route one invocation payload to its orchestration and emit the run stream.

    Payload fields (all optional unless noted):
      question   (str, required) — the user request
      mode       (str)  — explicit override: SYNTHESIS|DEBATE|ANALYSIS (skips router)
      evidence   (str)  — retrieved context block (Panel/Ask)
      roster     (list) — Panel members [{tier,label,max_tokens}, ...]
      adjudicator(dict) — Panel adjudicator {tier,label,max_tokens}
      router     (dict) — routing model {tier,label,max_tokens}
      generator  (dict) — Analyze codegen model {tier,label,max_tokens}
      code       (str)  — Analyze re-run: user-edited code (skips generation)

    Returns a small result dict (the chosen mode + any orchestration return value).
    """
    question = (payload.get("question") or "").strip()
    if not question and not payload.get("code"):
        raise InvocationError("payload has no question")

    router_cfg = payload.get("router") or {"tier": "oss", "label": "router", "max_tokens": 5}
    mode = run_router(
        backend=backend,
        meter=meter,
        emit=emit,
        question=question,
        router=router_cfg,
        override=payload.get("mode"),
    )

    if mode == "DEBATE":
        roster = payload.get("roster")
        adjudicator = payload.get("adjudicator")
        if not roster or not adjudicator:
            raise InvocationError("DEBATE requires a roster and an adjudicator")
        reviews = run_panel(
            backend=backend,
            meter=meter,
            emit=emit,
            question=question,
            evidence=payload.get("evidence", ""),
            roster=roster,
            adjudicator=adjudicator,
            review_system=payload.get("review_system", REVIEW_SYSTEM),
            adjudicate_system=ADJUDICATE_SYSTEM,
        )
        return {"mode": mode, "reviews": reviews}

    if mode == "ANALYSIS":
        if code_runner is None:
            raise InvocationError("ANALYSIS requires a code runner")
        result = run_analyze(
            backend=backend,
            runner=code_runner,
            meter=meter,
            emit=emit,
            question=question,
            analyze_system=ANALYZE_SYSTEM,
            generator=payload.get("generator"),
            code=payload.get("code"),
        )
        return {"mode": mode, "is_error": result.is_error}

    # SYNTHESIS (Ask). A single cited synthesis over the evidence — one Converse
    # streamed as an answer. (Tier 0 normally runs Ask browser-direct; routing here
    # to ANALYSIS/DEBATE is the agent's reason to exist, but it answers Ask too.)
    gen = payload.get("generator") or {"tier": "oss", "label": "ask", "max_tokens": 1024}
    prompt = f"Evidence:\n{payload.get('evidence', '')}\n\nQuestion: {question}"
    text, usage, _ = backend.converse(gen["tier"], _ASK_SYSTEM, prompt, gen["max_tokens"])
    meter.add_llm("ask", gen["tier"], gen["label"], usage)
    emit({"type": "cost", "total": round(meter.total, 6)})
    emit({"type": "answer", "text": text})
    return {"mode": mode}


_ASK_SYSTEM = (
    "Answer the question using only the provided evidence. Cite each supporting "
    "source by its exact identifier. If the evidence does not contain the answer, "
    "say so rather than guessing."
)
