"""Pure self-budgeting research loop (#248, Canvas move #5) — the agent-cell brain.

This is the reasoning core an agent cell runs on AgentCore Runtime: given a question and a
cost/time/step cap, plan and execute a bounded sequence of governed searches + fetches, then
report the best answer WITHIN the envelope. It is deliberately pure (no AWS, no network, no
wall-clock read) so the load-bearing guarantee — a capped agent cannot overspend no matter how it
behaves — is exhaustively unit-testable. The container (`agent/`) injects Bedrock/gateway-backed
`plan`/`search`/`fetch`/`clock`; tests inject fakes.

Two guarantees are kept deliberately separate (design "hard parts"):

  * SELF-GOVERNANCE (advisory): the planner is TOLD its remaining budget/time so it can decide how
    many searches/fetches it can afford. This is a planning input, never the enforcement.
  * ENFORCEMENT (load-bearing): every priced action passes `agentcell.evaluate_step` — the SAME
    pre-call budget cascade with the cell cap as its most-specific node — BEFORE it fires. A
    confused or adversarial planner still cannot spend past the cap: the gate declines the next
    step, the loop stops, and a partial result is returned. Cost is exact/pre-call; the time cap is
    soft (we decline to START a step past the deadline, never kill one in flight).

Two structural invariants the loop enforces itself, so they can't be violated by a bad planner:

  * NO UNGOVERNED LAUNCH — `run_research` refuses to start unless `agentcell.is_enforceable` holds
    (a real cost/time/step cap or a real scope budget). A background agent that spends real money
    must be bounded (#248 review MED #3).
  * NO UNGOVERNED EGRESS — a URL may be FETCHED only if a prior governed `search` surfaced it. The
    planner cannot fabricate a URL to dereference: the only fetchable set is `known_urls`,
    accumulated from search results, and `fetch` is the sole egress edge (in production the
    web-fetch SSRF/allowlist guard). So web-search opens no new egress path — every fetch still
    goes through web-fetch (#248 review follow-up, made a structural invariant, not a comment).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from cost.precall import evaluate_priced_cascade

from agate.agentcell import (
    AgentCellCap,
    cap_cascade_nodes,
    cost_remaining,
    deadline_reached,
    evaluate_step,
    is_enforceable,
    time_remaining,
)

# A backstop on iterations independent of the caps: even an all-scope-budget launch (no cell
# step cap) must terminate if the planner never answers. The cost/time caps normally bind first;
# this only catches a planner that loops on free/near-free actions.
MAX_ITERATIONS = 64


class ResearchError(ValueError):
    """The loop cannot start or continue safely (ungoverned launch, ungoverned egress). Fail
    closed — no action fires."""


ActionKind = Literal["search", "fetch", "answer"]


@dataclass(frozen=True, slots=True)
class Action:
    """The planner's chosen next move. `price_usd` is the quoted worst-case cost of a `search`/
    `fetch` (the answer is free); `query`/`url`/`text` carry the operand for the kind."""

    kind: ActionKind
    query: str = ""  # for a search
    url: str = ""  # for a fetch — MUST already be in known_urls (governed egress)
    price_usd: float = 0.0  # worst-case cost of this action; checked against the cascade
    text: str = ""  # for an answer — the agent's synthesis within the envelope


@dataclass(frozen=True, slots=True)
class PlanContext:
    """What the planner sees each step — its self-governance inputs. `cost_remaining`/
    `time_remaining` are None on an axis with no cap (unbounded on that axis). `known_urls` is the
    ONLY set the planner may fetch from; `findings` is the evidence gathered so far."""

    question: str
    cost_remaining: float | None
    time_remaining: float | None
    steps_taken: int
    findings: tuple[str, ...]
    known_urls: tuple[str, ...]


# Injected edges. `plan` is the LLM decision (a priced model call in production, but its own cost
# is folded into the action prices the planner quotes). `search`/`fetch` are the governed gateway
# tools; `clock` returns monotonic seconds (injected so the module reads no wall clock).
Planner = Callable[[PlanContext], Action]
Searcher = Callable[[str], list[str]]  # query -> candidate URLs (governed web-search)
Fetcher = Callable[[str], str]  # url -> content (governed web-fetch)
Clock = Callable[[], float]


# The self-governance contract handed to the reasoning model: it MUST reply with one JSON action.
# The prices are what the model BELIEVES each action costs (planning); enforcement re-prices and
# re-checks server-side regardless — a lying/confused price cannot overspend (the gate is the
# authority). Kept here so the prompt and the parser that consumes it stay in one place.
PLANNER_SYSTEM = (
    "You are a research agent working under a strict budget/time cap. Each turn, reply with "
    "EXACTLY ONE JSON object and nothing else, choosing your next action:\n"
    '  {"action":"search","query":"...","price_usd":0.005}  — find sources for a query\n'
    '  {"action":"fetch","url":"...","price_usd":0.001}       — read ONE url from known_urls\n'
    '  {"action":"answer","text":"..."}                        — finish with your best answer\n'
    "You are TOLD your remaining budget and time; spend them wisely and stop with an `answer` "
    "before you run out. You may only `fetch` a url that appeared in known_urls (from a prior "
    "search). If you are near the cap, answer with what you have — a partial answer is success."
)


def parse_planner_reply(text: str, *, search_price: float, fetch_price: float) -> Action:
    """Parse the reasoning model's one-JSON-action reply into an `Action`, fail-closed, stamping the
    action with the TRUSTED tool price — NOT the price the model quoted. Enforcement must not depend
    on a number the planner controls (a lying/confused planner could quote $0 to slip a real cost),
    so the model's `price_usd` is advisory (used only for its own planning); the loop gates on
    `search_price`/`fetch_price`, the prices the governed tools actually charge.

    A malformed or unrecognised reply becomes an `answer` carrying the raw text — so a confused
    planner ENDS the run (returning what it said) rather than looping or taking an unpriced action.
    Pure."""
    raw = (text or "").strip()
    # Tolerate a fenced ```json block or leading prose before the object.
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start : end + 1]
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return Action(kind="answer", text=(text or "").strip())
    if not isinstance(obj, dict):
        return Action(kind="answer", text=(text or "").strip())
    action = str(obj.get("action", "")).lower()
    if action == "search":
        return Action(kind="search", query=str(obj.get("query", "")), price_usd=search_price)
    if action == "fetch":
        return Action(kind="fetch", url=str(obj.get("url", "")), price_usd=fetch_price)
    if action == "answer":
        return Action(kind="answer", text=str(obj.get("text", "")))
    # Unknown action → end the run with the raw text (fail closed, no unpriced action fires).
    return Action(kind="answer", text=(text or "").strip())


@dataclass(frozen=True, slots=True)
class ResearchStep:
    """One executed (or refused) step, for the receipt: what was attempted, its price, whether the
    gate allowed it, and the cumulative spend after it. A refused step (`allowed=False`) is the
    one that hit the cap — recorded so the receipt is honest about where the boundary fell."""

    kind: ActionKind
    detail: str  # the query or url
    price_usd: float
    allowed: bool
    reason: str
    spent_after: float


@dataclass(frozen=True, slots=True)
class ResearchResult:
    """The frozen outcome of a capped research run. `cap_bounded` is True when the loop stopped
    because a cap was reached (a partial answer — success, not error, per the design), False when
    the agent answered within the envelope. The receipt fields make the boundary honest."""

    answer: str
    cap_bounded: bool
    stop_reason: str
    spent_usd: float
    elapsed_seconds: float
    steps_taken: int
    steps: tuple[ResearchStep, ...] = field(default_factory=tuple)

    def receipt(self) -> dict:
        """A compact receipt dict (actual spend/time/steps vs. the boundary) for the run event —
        mirrors the CostMeter receipt shape the SPA already renders."""
        return {
            "type": "receipt",
            "kind": "agent-cell",
            "answer_cap_bounded": self.cap_bounded,
            "stop_reason": self.stop_reason,
            "spent_usd": round(self.spent_usd, 6),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "steps_taken": self.steps_taken,
        }


def run_research(
    *,
    question: str,
    cap: AgentCellCap,
    scope_nodes: list[tuple[str, float, float | None]],
    plan: Planner,
    search: Searcher,
    fetch: Fetcher,
    clock: Clock,
    plan_price_usd: float = 0.0,
    max_iterations: int = MAX_ITERATIONS,
) -> ResearchResult:
    """Run a bounded, self-budgeting research loop and return the best answer within the cap.

    Refuses to START unless the launch is GOVERNED (`is_enforceable`) — a background agent with no
    enforceable bound is rejected (fail closed), never started. Then, each iteration:

      1. GATE THE PLANNING CALL: the reasoning-model turn is itself a priced action
         (`plan_price_usd`, the worst-case cost of one Converse). Gate it against the cell cap +
         scope cascade BEFORE calling the planner — so the model's own (and unboundedly growing)
         spend counts against the cap, not just the tool actions. If it would breach → stop with
         the partial answer (the cost cap is the TRUE spend ceiling, review #248-PR3 Finding 1).
      2. Ask the planner for the next Action, handing it its remaining budget/time (self-governance
         input) and the evidence + fetchable URLs so far.
      3. `answer` → stop, return it (answered within the envelope; not cap-bounded).
      4. `fetch` of a URL NOT surfaced by a prior search → refuse (ungoverned egress, fail closed).
      5. ENFORCE: run `evaluate_step` for the action's TRUSTED price against the cell cap + every
         scope node. If it rejects → stop with the partial answer so far (cap-bounded); the gate,
         not the planner, is the authority.
      6. Execute the governed search/fetch, accumulate spend/evidence, and continue.

    `clock()` is sampled to measure elapsed time (soft cap). `scope_nodes` is the family
    budget snapshot at launch (broad→specific); the cell's own running spend is carried as the
    most-specific cascade node. Pure — all AWS/network/LLM lives behind the injected edges."""
    if not is_enforceable(cap, scope_nodes):
        raise ResearchError(
            "refusing to launch an ungoverned agent cell: set a cost, time, or step cap "
            "(no enforceable bound present)"
        )

    t0 = clock()
    spent = 0.0
    steps: list[ResearchStep] = []
    findings: list[str] = []
    known_urls: list[str] = []
    best_answer = ""

    for _ in range(max(1, max_iterations)):
        elapsed = max(0.0, clock() - t0)

        # (1) The planning call is itself a priced/timed action — gate it FIRST, so the reasoning
        # model's own spend can never exceed the cell cap (Finding 1). Time is checked here too so
        # we never START a planning call past the deadline (soft cap, mirrors the tool step).
        if deadline_reached(cap, elapsed):
            return ResearchResult(
                answer=best_answer,
                cap_bounded=True,
                stop_reason=f"time cap reached ({cap.seconds}s)",
                spent_usd=spent,
                elapsed_seconds=elapsed,
                steps_taken=len(steps),
                steps=tuple(steps),
            )
        plan_nodes = cap_cascade_nodes(cap, spent, scope_nodes)
        plan_gate = evaluate_priced_cascade(price_usd=plan_price_usd, nodes=plan_nodes)
        if plan_gate.decision != "allow":
            return ResearchResult(
                answer=best_answer,
                cap_bounded=True,
                stop_reason=f"budget cap reached before planning: {plan_gate.reason}",
                spent_usd=spent,
                elapsed_seconds=elapsed,
                steps_taken=len(steps),
                steps=tuple(steps),
            )
        spent = round(spent + max(0.0, plan_price_usd), 6)

        ctx = PlanContext(
            question=question,
            cost_remaining=cost_remaining(cap, spent),
            time_remaining=time_remaining(cap, elapsed),
            steps_taken=len(steps),
            findings=tuple(findings),
            known_urls=tuple(known_urls),
        )
        action = plan(ctx)

        if action.kind == "answer":
            return ResearchResult(
                answer=action.text,
                cap_bounded=False,
                stop_reason="answered within envelope",
                spent_usd=spent,
                elapsed_seconds=elapsed,
                steps_taken=len(steps),
                steps=tuple(steps),
            )

        # Governed-egress invariant: a fetch target must have been surfaced by a prior governed
        # search. A planner-fabricated URL is refused BEFORE the gate — it never reaches `fetch`,
        # so web-search opens no egress path web-fetch didn't already permit.
        if action.kind == "fetch" and action.url not in known_urls:
            raise ResearchError(
                f"refusing to fetch a URL not surfaced by a governed search: {action.url!r}"
            )

        # ENFORCEMENT: the cell cap + scope cascade decide whether this priced step may start —
        # regardless of what the planner wants. Recompute elapsed at the decision point.
        elapsed = max(0.0, clock() - t0)
        decision = evaluate_step(
            cap=cap,
            next_price_usd=action.price_usd,
            spent_usd=spent,
            steps_taken=len(steps),
            elapsed_seconds=elapsed,
            scope_nodes=scope_nodes,
        )
        if not decision.allowed:
            steps.append(
                ResearchStep(
                    kind=action.kind,
                    detail=action.query or action.url,
                    price_usd=action.price_usd,
                    allowed=False,
                    reason=decision.reason,
                    spent_after=spent,
                )
            )
            return ResearchResult(
                answer=best_answer,
                cap_bounded=True,
                stop_reason=decision.reason,
                spent_usd=spent,
                elapsed_seconds=elapsed,
                steps_taken=len(steps),
                steps=tuple(steps),
            )

        # Allowed → execute the governed action and record its cost as spent.
        spent = round(spent + max(0.0, action.price_usd), 6)
        if action.kind == "search":
            for url in search(action.query):
                if url not in known_urls:
                    known_urls.append(url)
            findings.append(f"searched: {action.query}")
        else:  # fetch — url already validated to be in known_urls above
            content = fetch(action.url)
            findings.append(content)
            best_answer = content  # best-effort partial: the latest evidence gathered
        steps.append(
            ResearchStep(
                kind=action.kind,
                detail=action.query or action.url,
                price_usd=action.price_usd,
                allowed=True,
                reason="within cap",
                spent_after=spent,
            )
        )

    # Iteration backstop: the planner never answered and never hit a cap on a priced axis.
    return ResearchResult(
        answer=best_answer,
        cap_bounded=True,
        stop_reason=f"iteration backstop reached ({max(1, max_iterations)})",
        spent_usd=spent,
        elapsed_seconds=max(0.0, clock() - t0),
        steps_taken=len(steps),
        steps=tuple(steps),
    )
