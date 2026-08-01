"""Container-side agent-cell runner (#248, Canvas move #5) — assemble + run the research loop.

`agent/server.py` routes a capped payload here. This module is the thin AWS/edge shim that turns a
decoded agent-cell invocation into a `agate.research_loop.run_research` call: it builds the
`AgentCellCap` from the payload, the family budget cascade snapshot from the verified scope, the
Bedrock-backed planner, and the governed search/fetch edges (via `agent.research_client`, which
forwards the verified token to the web-search / web-fetch tool Lambdas). All the enforcement lives
in the pure `agate` layer; here we only wire the injected edges and emit the run events the SPA
renders. Kept out of `agate/` so the pure suite stays AWS-free.

The scope-budget snapshot is a PLANNING FLOOR read once at launch (the live per-step debit still
happens inside the web-search / web-fetch tools' own cascades on each call); the cell cap is the
authoritative most-specific node the loop enforces pre-step.
"""

from __future__ import annotations

import os
import time
from typing import Any

from agate.agentcell import AgentCellCap
from agate.research_loop import (
    PLANNER_SYSTEM,
    Action,
    PlanContext,
    ResearchError,
    parse_planner_reply,
    run_research,
)
from agate.tags import claims_to_tags

from agent import research_client

# Trusted per-action prices the governed tools charge — the loop gates on THESE, never a
# planner-quoted number. Mirror the web-search / web-fetch handler defaults; an institution tunes
# them via the same env the tools read.
SEARCH_PRICE_USD = 0.005
FETCH_PRICE_USD = 0.001
# The reasoning model gets a small output budget per planning turn (it emits one JSON action).
PLAN_MAX_TOKENS = 256
# A worst-case per-planning-turn model cost, gated against the cell cap BEFORE each Converse so the
# reasoning-model spend counts toward the cap (not just the tool actions — review PR3 Finding 1).
# A coarse upper bound (input grows with accumulated findings, capped by MAX_PROMPT_FINDING_CHARS
# below); an institution can tune it. The authoritative post-hoc meter still bills exact spend.
PLAN_PRICE_USD = float(os.environ.get("AGATE_AGENTCELL_PLAN_PRICE_USD", "0.02"))
# Cap how much accumulated evidence is re-sent to the planner each turn, so per-turn input tokens
# (and thus planning cost) can't grow unbounded across a long run (review PR3 Finding 1).
MAX_PROMPT_FINDING_CHARS = 4000


def _cap_from_payload(cap: Any) -> AgentCellCap:
    """Build an `AgentCellCap` from the payload's `cap` object, coercing each axis to the right
    type (or None if absent/unparseable). A garbled cap becomes an all-None cap — which the loop
    then REFUSES to launch (fail closed) unless a scope budget makes it enforceable."""
    if not isinstance(cap, dict):
        return AgentCellCap()
    return AgentCellCap(
        cost_usd=_as_float(cap.get("cost_usd")),
        seconds=_as_float(cap.get("seconds")),
        max_steps=_as_int(cap.get("max_steps")),
    )


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _as_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError, OverflowError):
        # OverflowError: a JSON non-finite (e.g. 1e400 → inf) reaching int() — treat as absent
        # (all-None cap), which the loop then refuses unless a scope budget makes it enforceable.
        return None


def _make_planner(backend: Any, tier: str):
    """Build the loop's `plan(ctx) -> Action` edge: a Bedrock Converse call that returns ONE JSON
    action, parsed fail-closed into an Action stamped with the TRUSTED tool prices (never the
    price the model quotes). A backend failure ends the run with an honest error answer."""

    def plan(ctx: PlanContext) -> Action:
        prompt = _plan_prompt(ctx)
        try:
            text, usage, _ = backend.converse(tier, PLANNER_SYSTEM, prompt, PLAN_MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001 — a planner failure ends the run, never crashes it
            return Action(kind="answer", text=f"[agent-cell] planner error: {exc}")
        return parse_planner_reply(text, search_price=SEARCH_PRICE_USD, fetch_price=FETCH_PRICE_USD)

    return plan


def _plan_prompt(ctx: PlanContext) -> str:
    """Render the planner's self-governance view — question, remaining budget/time, evidence, and
    the ONLY urls it may fetch — into the user turn."""
    cost = "unbounded" if ctx.cost_remaining is None else f"${ctx.cost_remaining:.4f}"
    secs = "unbounded" if ctx.time_remaining is None else f"{ctx.time_remaining:.0f}s"
    urls = "\n".join(f"  - {u}" for u in ctx.known_urls) or "  (none yet — search first)"
    # Bound the evidence re-sent each turn so per-turn input tokens (and planning cost) can't grow
    # unbounded across a long run (review PR3 Finding 1). The full findings still drive the answer.
    findings_block = "\n".join(f"  - {f}" for f in ctx.findings) or "  (none yet)"
    if len(findings_block) > MAX_PROMPT_FINDING_CHARS:
        findings_block = findings_block[:MAX_PROMPT_FINDING_CHARS] + "\n  … (truncated)"
    findings = findings_block
    return (
        f"Question: {ctx.question}\n"
        f"Remaining budget: {cost}   Remaining time: {secs}   Steps taken: {ctx.steps_taken}\n"
        f"known_urls (the ONLY urls you may fetch):\n{urls}\n"
        f"findings so far:\n{findings}\n"
        "Reply with your next action as one JSON object."
    )


def run_agent_cell(
    payload: dict,
    *,
    backend: Any,
    tier: str,
    entitled: list[str],
    emit,
    clock=time.monotonic,
    scope_reader=None,
) -> None:
    """Run one agent-cell invocation and emit its run events (progress + terminal receipt).

    Derives the cap + verified scope from the payload, refuses an ungoverned launch, then runs the
    self-budgeting loop under the governed edges. Fail-closed: any launch refusal or verification
    error surfaces as an answer event + a receipt, never an unbounded run."""
    idp_token = payload.get("idp_token", "")
    question = (payload.get("question") or "").strip()
    if not question:
        emit({"type": "answer", "title": "error", "text": "agent cell has no question"})
        return

    cap = _cap_from_payload(payload.get("cap"))
    scope_nodes = _scope_nodes(idp_token, scope_reader)

    search = research_client.make_search(idp_token)
    fetch = research_client.make_fetch(idp_token)
    # The planner runs on the cheapest ENTITLED model (a concrete Bedrock id, never the logical
    # tier label — Bedrock rejects a bare label), mirroring server._resolve_models. No entitled
    # model → fail closed (an unverifiable token yields no entitlement).
    if not entitled:
        emit({"type": "answer", "title": "error", "text": "agent cell: no entitled model"})
        return
    plan = _make_planner(backend, entitled[0])

    try:
        result = run_research(
            question=question,
            cap=cap,
            scope_nodes=scope_nodes,
            plan=plan,
            search=search,
            fetch=fetch,
            clock=clock,
            plan_price_usd=PLAN_PRICE_USD,
        )
    except ResearchError as exc:
        # An ungoverned launch (no enforceable bound) or ungoverned egress attempt — fail closed.
        emit({"type": "answer", "title": "error", "text": f"agent cell refused: {exc}"})
        return

    if result.cap_bounded:
        emit(
            {
                "type": "answer",
                "title": "partial (cap-bounded)",
                "text": result.answer
                or f"Stopped at the cap ({result.stop_reason}) before an answer was reached.",
            }
        )
    else:
        emit({"type": "answer", "text": result.answer})
    emit(result.receipt())


def _scope_nodes(idp_token: str, scope_reader) -> list[tuple[str, float, float | None]]:
    """Build the family budget cascade snapshot from the VERIFIED token's tenant/scope. Returns []
    when there is no verifiable scope or no reader wired — the loop then requires the cell cap
    itself to be enforceable (fail closed), never an unbounded launch. `scope_reader(tenant, scope)
    -> [(label, spend, budget), ...]` is injected (the DynamoDB edge); None → no scope floor."""
    if not idp_token or scope_reader is None:
        return []
    try:
        from agate.jwt_verify import config_from_env, verify_token

        claims = verify_token(idp_token, **config_from_env())
        tags = claims_to_tags(claims)
    except Exception:  # noqa: BLE001 — an unverifiable token yields no scope floor (fail closed)
        return []
    try:
        return scope_reader(tags.tenant, tags.scope)
    except Exception:  # noqa: BLE001 — a reader failure yields no floor; the cell cap must bind
        return []
