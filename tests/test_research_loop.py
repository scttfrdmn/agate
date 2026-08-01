"""Agent-cell research-loop tests (#248, Canvas move #5). Pure — no agent, no network, no clock.

The load-bearing guarantee: a capped research agent cannot overspend or reach ungoverned egress no
matter how its planner behaves. These drive the loop with adversarial fake planners and assert the
gate — not the planner — is the authority: the cap stops the loop, an ungoverned launch is refused,
and a fabricated fetch URL never leaves.
"""

from __future__ import annotations

import pytest
from agate.agentcell import AgentCellCap
from agate.research_loop import (
    Action,
    PlanContext,
    ResearchError,
    parse_planner_reply,
    run_research,
)

SCOPE_OK: list[tuple[str, float, float | None]] = [("tenant", 0.0, 1000.0)]


def _clock_from(ticks):
    """A fake monotonic clock that returns successive values from `ticks`, holding the last."""
    seq = list(ticks)
    state = {"i": 0}

    def clock() -> float:
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[i]

    return clock


def _never_called(*_a, **_k):
    raise AssertionError("edge should not be called")


def test_refuses_ungoverned_launch():
    # MED #3 as a structural invariant: an all-None cap with no scope budget never starts.
    with pytest.raises(ResearchError, match="ungoverned"):
        run_research(
            question="scan the literature",
            cap=AgentCellCap(),
            scope_nodes=[],
            plan=_never_called,
            search=_never_called,
            fetch=_never_called,
            clock=lambda: 0.0,
        )


def test_answers_within_envelope_is_not_cap_bounded():
    def plan(_ctx: PlanContext) -> Action:
        return Action(kind="answer", text="here is the answer")

    r = run_research(
        question="q",
        cap=AgentCellCap(cost_usd=2.0),
        scope_nodes=SCOPE_OK,
        plan=plan,
        search=_never_called,
        fetch=_never_called,
        clock=lambda: 0.0,
    )
    assert r.answer == "here is the answer"
    assert r.cap_bounded is False
    assert r.stop_reason == "answered within envelope"
    assert r.spent_usd == 0.0


def test_cost_cap_stops_the_loop_regardless_of_planner_intent():
    # An adversarial planner that ALWAYS wants another $0.60 search. The $2 cell cap must stop it.
    def plan(_ctx: PlanContext) -> Action:
        return Action(kind="search", query="more", price_usd=0.60)

    r = run_research(
        question="q",
        cap=AgentCellCap(cost_usd=2.0),
        scope_nodes=SCOPE_OK,
        plan=plan,
        search=lambda _q: [],
        fetch=_never_called,
        clock=lambda: 0.0,
    )
    # 0.60 * 3 = 1.80 <= 2.0 allowed; the 4th (would reach 2.40) is refused. Cap held.
    assert r.cap_bounded is True
    assert r.spent_usd == pytest.approx(1.80)
    allowed_steps = [s for s in r.steps if s.allowed]
    assert len(allowed_steps) == 3
    assert r.steps[-1].allowed is False  # the refused step is recorded (honest boundary)


def test_scope_budget_below_cell_cap_still_binds():
    # Generous cell cap ($100), but the tenant has only $1 — the cascade rejects at the tenant.
    tight = [("tenant", 0.99, 1.0)]

    def plan(_ctx: PlanContext) -> Action:
        return Action(kind="search", query="x", price_usd=0.50)

    r = run_research(
        question="q",
        cap=AgentCellCap(cost_usd=100.0),
        scope_nodes=tight,
        plan=plan,
        search=lambda _q: [],
        fetch=_never_called,
        clock=lambda: 0.0,
    )
    assert r.cap_bounded is True
    assert r.spent_usd == 0.0  # nothing fired; the very first step was refused at the tenant node


def test_time_cap_declines_to_start_after_deadline():
    # Soft time cap: the planner wants a step, but the clock is past the 300s envelope → stop.
    def plan(_ctx: PlanContext) -> Action:
        return Action(kind="search", query="x", price_usd=0.0)

    r = run_research(
        question="q",
        cap=AgentCellCap(seconds=300.0),
        scope_nodes=SCOPE_OK,
        # t0=0, then the elapsed check reads 301 → past the deadline before any step starts.
        clock=_clock_from([0.0, 301.0, 301.0]),
        plan=plan,
        search=_never_called,
        fetch=_never_called,
    )
    assert r.cap_bounded is True
    assert "time cap" in r.stop_reason


def test_refuses_to_fetch_a_url_not_surfaced_by_search():
    # Governed-egress invariant: a planner-fabricated URL is refused BEFORE the fetch edge.
    def plan(_ctx: PlanContext) -> Action:
        return Action(kind="fetch", url="https://evil.example/x", price_usd=0.01)

    with pytest.raises(ResearchError, match="not surfaced by a governed search"):
        run_research(
            question="q",
            cap=AgentCellCap(cost_usd=5.0),
            scope_nodes=SCOPE_OK,
            plan=plan,
            search=_never_called,
            fetch=_never_called,  # must never be reached
            clock=lambda: 0.0,
        )


def test_only_searched_urls_are_fetchable_then_answers():
    # Search surfaces a URL; the planner may then fetch THAT url (governed egress), then answer.
    calls = {"n": 0}
    surfaced = "https://search.example.edu/paper/1"

    def plan(ctx: PlanContext) -> Action:
        calls["n"] += 1
        if calls["n"] == 1:
            return Action(kind="search", query="kinetics", price_usd=0.02)
        if calls["n"] == 2:
            # The planner may only fetch what search surfaced — assert it's visible in context.
            assert surfaced in ctx.known_urls
            return Action(kind="fetch", url=surfaced, price_usd=0.05)
        return Action(kind="answer", text="synthesised from the fetched paper")

    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return "paper contents"

    r = run_research(
        question="q",
        cap=AgentCellCap(cost_usd=5.0),
        scope_nodes=SCOPE_OK,
        plan=plan,
        search=lambda _q: [surfaced],
        fetch=fetch,
        clock=lambda: 0.0,
    )
    assert fetched == [surfaced]  # the ONLY url dereferenced is the one search surfaced
    assert r.cap_bounded is False
    assert r.answer == "synthesised from the fetched paper"
    assert r.spent_usd == pytest.approx(0.07)


def test_partial_answer_is_last_evidence_when_cap_hit():
    # A cap-bounded run returns the best-effort partial (latest fetched evidence), not empty.
    surfaced = "https://search.example.edu/p"
    calls = {"n": 0}

    def plan(ctx: PlanContext) -> Action:
        calls["n"] += 1
        if calls["n"] == 1:
            return Action(kind="search", query="q", price_usd=0.10)
        if calls["n"] == 2:
            return Action(kind="fetch", url=surfaced, price_usd=0.10)
        # Third step: wants another fetch that would breach the $0.25 cap (0.20 + 0.10 > 0.25).
        return Action(kind="fetch", url=surfaced, price_usd=0.10)

    r = run_research(
        question="q",
        cap=AgentCellCap(cost_usd=0.25),
        scope_nodes=SCOPE_OK,
        plan=plan,
        search=lambda _q: [surfaced],
        fetch=lambda _u: "the evidence",
        clock=lambda: 0.0,
    )
    assert r.cap_bounded is True
    assert r.answer == "the evidence"  # partial, not empty
    assert r.spent_usd == pytest.approx(0.20)


def test_iteration_backstop_terminates_a_free_looping_planner():
    # A planner that only issues free searches never hits a cost/time cap — the backstop stops it.
    def plan(_ctx: PlanContext) -> Action:
        return Action(kind="search", query="free", price_usd=0.0)

    r = run_research(
        question="q",
        cap=AgentCellCap(max_steps=None, seconds=None, cost_usd=None),
        scope_nodes=SCOPE_OK,  # enforceable via the scope budget, so it launches
        plan=plan,
        search=lambda _q: [],
        fetch=_never_called,
        clock=lambda: 0.0,
        max_iterations=5,
    )
    assert r.cap_bounded is True
    assert "iteration backstop" in r.stop_reason
    assert r.steps_taken == 5


# --- planner reply parsing (self-governance input; enforcement uses TRUSTED prices) ---------


def test_parse_search_stamps_trusted_price_not_the_quoted_one():
    # The model quotes $0 (adversarial), but the parser stamps the trusted tool price — the gate
    # can never be fooled by a planner-controlled number.
    a = parse_planner_reply(
        '{"action":"search","query":"kinetics","price_usd":0.0}',
        search_price=0.005,
        fetch_price=0.001,
    )
    assert a.kind == "search" and a.query == "kinetics"
    assert a.price_usd == 0.005  # trusted, not the quoted 0.0


def test_parse_fetch_uses_trusted_fetch_price():
    a = parse_planner_reply(
        '{"action":"fetch","url":"https://a/1","price_usd":99.0}',
        search_price=0.005,
        fetch_price=0.001,
    )
    assert a.kind == "fetch" and a.url == "https://a/1"
    assert a.price_usd == 0.001


def test_parse_answer_and_tolerates_fenced_prose():
    a = parse_planner_reply(
        'here is my choice:\n```json\n{"action":"answer","text":"done"}\n```',
        search_price=0.005,
        fetch_price=0.001,
    )
    assert a.kind == "answer" and a.text == "done"


def test_parse_malformed_reply_ends_run_with_raw_text():
    # A non-JSON / unknown-action reply becomes an answer (fail closed — never an unpriced action).
    for bad in ("not json", '{"action":"delete_everything"}', "[]", ""):
        a = parse_planner_reply(bad, search_price=0.005, fetch_price=0.001)
        assert a.kind == "answer"


def test_planning_call_cost_counts_against_the_cap():
    # Review PR3 Finding 1: the reasoning-model call is itself priced and gated BEFORE it fires, so
    # the model's own spend counts toward the cap — not just the tool actions. With plan_price=$0.40
    # and a $1.00 cap, only 2 planning turns fit ($0.80); the 3rd is refused before calling plan().
    plan_calls = {"n": 0}

    def plan(_ctx: PlanContext) -> Action:
        plan_calls["n"] += 1
        return Action(kind="search", query="x", price_usd=0.0)  # free tool action

    r = run_research(
        question="q",
        cap=AgentCellCap(cost_usd=1.0),
        scope_nodes=SCOPE_OK,
        plan=plan,
        search=lambda _q: [],
        fetch=_never_called,
        clock=lambda: 0.0,
        plan_price_usd=0.40,
    )
    assert r.cap_bounded is True
    assert "before planning" in r.stop_reason
    assert plan_calls["n"] == 2  # the 3rd planning call was gated out (2 * 0.40 = 0.80 <= 1.0)
    assert r.spent_usd == pytest.approx(0.80)


def test_planning_gate_respects_time_cap_before_calling_planner():
    # The planning call also honors the soft time cap: past the deadline, no new planning starts.
    def plan(_ctx: PlanContext) -> Action:
        raise AssertionError("planner must not be called past the deadline")

    r = run_research(
        question="q",
        cap=AgentCellCap(seconds=300.0),
        scope_nodes=SCOPE_OK,
        plan=plan,
        search=_never_called,
        fetch=_never_called,
        clock=_clock_from([0.0, 301.0, 301.0]),
        plan_price_usd=0.01,
    )
    assert r.cap_bounded is True
    assert "time cap" in r.stop_reason


def test_receipt_reports_honest_boundary():
    def plan(_ctx: PlanContext) -> Action:
        return Action(kind="answer", text="done")

    r = run_research(
        question="q",
        cap=AgentCellCap(cost_usd=1.0),
        scope_nodes=SCOPE_OK,
        plan=plan,
        search=_never_called,
        fetch=_never_called,
        clock=_clock_from([0.0, 2.5]),
    )
    rc = r.receipt()
    assert rc["type"] == "receipt" and rc["kind"] == "agent-cell"
    assert rc["answer_cap_bounded"] is False
    assert rc["steps_taken"] == 0
    assert rc["spent_usd"] == 0.0
