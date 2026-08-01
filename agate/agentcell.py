"""Agent-cell cap enforcement (#248, Canvas move #5) — pure, fail-closed.

The load-bearing claim of the agent-cell design: a cell's budget/time cap is NOT advisory text in a
prompt (which an LLM can ignore under retry pressure) — it is ENFORCED by the same pre-call budget
cascade that gates every other priced action (`cost.precall.evaluate_priced_cascade`, #81). The
agent is *told* its remaining budget so it can plan, but enforcement never depends on the agent
behaving. This module builds the cell cap into that cascade and provides the soft time-cap rule, all
as pure functions so the guarantee is exhaustively unit-testable without a live agent.

Two guarantees, kept deliberately separate (design "hard parts"):
  * COST is enforced pre-call, exactly: a step is allowed only if its quoted price fits the cell cap
    AND every scope/tenant/user budget above it. The cell cap is the most-specific cascade node.
  * TIME is a SOFT bound: we can only decline to START a new step past the deadline (bounded overrun
    on an in-flight step) — mirroring the soft-cap "decline the next call" rule, not an in-flight
    kill. `time_remaining` / `deadline_reached` express this over an injected clock (no wall-clock
    read here, so it stays pure and testable).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cost.precall import CascadeResult, evaluate_priced_cascade

# The label used for the cell-cap node in the cascade — most-specific, so it's checked alongside the
# scope/tenant/user nodes and named if IT is the one that rejects.
CELL_CAP_LABEL = "agent-cell-cap"


@dataclass(frozen=True, slots=True)
class AgentCellCap:
    """A user-set cap on an agent cell. Either bound may be None (uncapped on that axis); at least
    one should be set for a governed run. `cost_usd` is the total dollars the cell may spend across
    all its steps; `seconds` is the wall-clock envelope; `max_steps` optionally bounds tool/model
    invocations (a coarse belt-and-braces bound independent of cost)."""

    cost_usd: float | None = None
    seconds: float | None = None
    max_steps: int | None = None


def cap_cascade_nodes(
    cap: AgentCellCap,
    spent_usd: float,
    scope_nodes: list[tuple[str, float, float | None]],
) -> list[tuple[str, float, float | None]]:
    """Build the cascade node list for the NEXT step of an agent cell: the caller's scope/tenant/
    user nodes (broad→specific, as any priced action uses) PLUS the cell cap as the most-specific
    node, carrying the cell's own running spend. So a step must fit the cell cap AND every budget
    above it — 'the cap is another node in the same cascade.' A cap of None on cost imposes no
    cell-level cap (the scope nodes still apply). Pure."""
    nodes = list(scope_nodes)
    nodes.append((CELL_CAP_LABEL, spent_usd, cap.cost_usd))
    return nodes


@dataclass(frozen=True, slots=True)
class StepDecision:
    """Whether the agent cell may START its next step, and why not. `allowed` is False when the
    cost cascade rejects (`cascade` names the breaching node), the time deadline is reached, or the
    step budget is exhausted — the three cap axes. `reason` is human-readable for the receipt."""

    allowed: bool
    reason: str
    cascade: CascadeResult | None = None


def evaluate_step(
    *,
    cap: AgentCellCap,
    next_price_usd: float,
    spent_usd: float,
    steps_taken: int,
    elapsed_seconds: float,
    scope_nodes: list[tuple[str, float, float | None]],
) -> StepDecision:
    """Decide whether the agent cell may START its next step — the ENFORCEMENT point (#248). Checks
    the three cap axes, fail-closed, independent of anything the agent 'planned':

      1. steps: if `max_steps` is set and already reached → stop.
      2. time (soft): if the deadline is reached, don't start a new step (bounded overrun on the
         in-flight one is accepted; we never kill mid-step).
      3. cost (hard, pre-call): the next step's quoted price must fit the cell cap AND every scope
         node — via the SAME `evaluate_priced_cascade` the chat/fetch/search paths use.

    Order: cheap local checks (steps, time) before the cascade. Pure over the injected
    `elapsed_seconds` (no clock read here)."""
    if cap.max_steps is not None and steps_taken >= cap.max_steps:
        return StepDecision(False, f"step cap reached ({cap.max_steps})")
    if deadline_reached(cap, elapsed_seconds):
        return StepDecision(False, f"time cap reached ({cap.seconds}s)")
    nodes = cap_cascade_nodes(cap, spent_usd, scope_nodes)
    result = evaluate_priced_cascade(price_usd=next_price_usd, nodes=nodes)
    if result.decision != "allow":
        where = "cell cap" if result.breaching_node == CELL_CAP_LABEL else result.breaching_node
        return StepDecision(
            False, f"budget cap reached at {where}: {result.reason}", cascade=result
        )
    return StepDecision(True, "within cap", cascade=result)


def is_enforceable(cap: AgentCellCap, scope_nodes: list[tuple[str, float, float | None]]) -> bool:
    """Whether an agent-cell launch is actually GOVERNED — refuse to start one that has no
    enforceable bound at all (#248 review): a background agent that spends real money must be
    capped. True if a finite cost cap, a finite time cap, a step cap, OR a scope node with a real
    (finite, >0) budget is present. An all-None cap with no scope budget is NOT enforceable → the
    caller must reject the launch (fail closed), not start an uncapped money-spending agent."""
    if cap.cost_usd is not None and math.isfinite(cap.cost_usd) and cap.cost_usd > 0:
        return True
    if cap.seconds is not None and math.isfinite(cap.seconds) and cap.seconds > 0:
        return True
    if cap.max_steps is not None and cap.max_steps > 0:
        return True
    return any(b is not None and math.isfinite(b) and b > 0 for _label, _spend, b in scope_nodes)


def deadline_reached(cap: AgentCellCap, elapsed_seconds: float) -> bool:
    """Soft time-cap rule: True once the wall-clock envelope is used up, so no NEW step starts.
    No time cap → never reached. A non-finite deadline or elapsed value is treated as REACHED
    (fail closed — a malformed time cap must not run forever). Pure over the injected elapsed."""
    if cap.seconds is None:
        return False
    if not (math.isfinite(cap.seconds) and math.isfinite(elapsed_seconds)):
        return True
    return elapsed_seconds >= cap.seconds


def time_remaining(cap: AgentCellCap, elapsed_seconds: float) -> float | None:
    """Seconds left in the time envelope (never negative), or None if no time cap — passed to the
    agent as PLANNING context (not enforcement). Pure."""
    if cap.seconds is None:
        return None
    return max(0.0, cap.seconds - max(0.0, elapsed_seconds))


def cost_remaining(cap: AgentCellCap, spent_usd: float) -> float | None:
    """Dollars left in the cost envelope (never negative), or None if no cost cap — the other
    PLANNING input handed to the agent so it can budget its own searches/calls. Pure."""
    if cap.cost_usd is None:
        return None
    if not math.isfinite(spent_usd):
        return 0.0
    return max(0.0, cap.cost_usd - max(0.0, spent_usd))
