"""Agent-cell cap enforcement tests (#248). Pure — no agent, no clock, no AWS.

The load-bearing guarantee: a cell's cost/time/step cap is ENFORCED by the pre-call cascade, not by
the agent behaving. These drive a step loop and assert it stops at the cap no matter what the agent
'wanted' to do next.
"""

from __future__ import annotations

from agate.agentcell import (
    CELL_CAP_LABEL,
    AgentCellCap,
    cap_cascade_nodes,
    cost_remaining,
    deadline_reached,
    evaluate_step,
    is_enforceable,
    time_remaining,
)

# A caller with plenty of scope/tenant/user headroom, so the CELL cap is the only binding limit.
SCOPE_OK: list[tuple[str, float, float | None]] = [("tenant", 0.0, 1000.0), ("user", 0.0, 1000.0)]


def test_cap_is_the_most_specific_cascade_node():
    nodes = cap_cascade_nodes(AgentCellCap(cost_usd=2.0), spent_usd=0.5, scope_nodes=SCOPE_OK)
    assert nodes[-1] == (CELL_CAP_LABEL, 0.5, 2.0)  # appended last = most specific
    assert nodes[:-1] == SCOPE_OK  # scope nodes preserved, broad→specific


def test_cost_cap_stops_the_loop_regardless_of_agent_intent():
    # Enforcement: run priced steps until the cell's $2 cap is exhausted. The "agent" always WANTS
    # another step; the gate stops it. Each step costs $0.60.
    cap = AgentCellCap(cost_usd=2.0)
    spent = 0.0
    steps = 0
    d = None
    while steps < 100:  # the agent would happily loop forever
        d = evaluate_step(
            cap=cap,
            next_price_usd=0.60,
            spent_usd=spent,
            steps_taken=steps,
            elapsed_seconds=0.0,
            scope_nodes=SCOPE_OK,
        )
        if not d.allowed:
            break
        spent += 0.60
        steps += 1
    # 0.60 * 3 = 1.80 ≤ 2.0 allowed; the 4th (would reach 2.40) is rejected. Cap held.
    assert steps == 3
    assert spent <= cap.cost_usd
    assert d is not None and "cell cap" in d.reason


def test_a_scope_budget_below_the_cell_cap_still_binds():
    # The cell cap is generous ($100) but the tenant has only $1 left — the cascade rejects at the
    # tenant node, named, before the cell cap is reached (a sub-agent can't drain the family).
    tight_scope = [("tenant", 0.99, 1.0), ("user", 0.0, 1000.0)]
    d = evaluate_step(
        cap=AgentCellCap(cost_usd=100.0),
        next_price_usd=0.50,
        spent_usd=0.0,
        steps_taken=0,
        elapsed_seconds=0.0,
        scope_nodes=tight_scope,
    )
    assert d.allowed is False
    assert d.cascade.breaching_node == "tenant"  # named the real limiter, not the cell cap


def test_time_cap_is_soft_declines_to_start_after_deadline():
    cap = AgentCellCap(seconds=300.0)
    # Before the deadline: a step may start even though it may overrun (bounded, in-flight).
    assert (
        evaluate_step(
            cap=cap,
            next_price_usd=0.01,
            spent_usd=0.0,
            steps_taken=0,
            elapsed_seconds=299.0,
            scope_nodes=SCOPE_OK,
        ).allowed
        is True
    )
    # At/after the deadline: no new step starts.
    d = evaluate_step(
        cap=cap,
        next_price_usd=0.01,
        spent_usd=0.0,
        steps_taken=0,
        elapsed_seconds=300.0,
        scope_nodes=SCOPE_OK,
    )
    assert d.allowed is False and "time cap" in d.reason


def test_step_cap_stops_the_loop():
    cap = AgentCellCap(max_steps=2)
    assert (
        evaluate_step(
            cap=cap,
            next_price_usd=0.0,
            spent_usd=0.0,
            steps_taken=1,
            elapsed_seconds=0.0,
            scope_nodes=SCOPE_OK,
        ).allowed
        is True
    )
    d = evaluate_step(
        cap=cap,
        next_price_usd=0.0,
        spent_usd=0.0,
        steps_taken=2,
        elapsed_seconds=0.0,
        scope_nodes=SCOPE_OK,
    )
    assert d.allowed is False and "step cap" in d.reason


def test_planning_inputs_remaining_budget_and_time():
    cap = AgentCellCap(cost_usd=2.0, seconds=300.0)
    assert cost_remaining(cap, 0.5) == 1.5
    assert cost_remaining(cap, 5.0) == 0.0  # never negative
    assert time_remaining(cap, 100.0) == 200.0
    assert time_remaining(cap, 999.0) == 0.0
    # Uncapped axes report None (no planning bound).
    assert cost_remaining(AgentCellCap(), 1.0) is None
    assert time_remaining(AgentCellCap(), 1.0) is None
    assert deadline_reached(AgentCellCap(), 1e9) is False


def test_negative_or_nan_price_fails_closed():
    # A malformed quoted price must never be allowed (cascade fails closed on non-finite/negative).
    d = evaluate_step(
        cap=AgentCellCap(cost_usd=10.0),
        next_price_usd=float("nan"),
        spent_usd=0.0,
        steps_taken=0,
        elapsed_seconds=0.0,
        scope_nodes=SCOPE_OK,
    )
    assert d.allowed is False


def test_nan_or_inf_cost_cap_fails_closed_not_open():
    # Review HIGH: a non-finite cap must NOT disable the cell node (x > nan is always False). With
    # NO scope nodes, a NaN/inf cost cap must still reject (fail closed), not allow unbounded steps.
    for bad in (float("nan"), float("inf")):
        d = evaluate_step(
            cap=AgentCellCap(cost_usd=bad),
            next_price_usd=0.10,
            spent_usd=0.0,
            steps_taken=0,
            elapsed_seconds=0.0,
            scope_nodes=[],
        )
        assert d.allowed is False


def test_is_enforceable_requires_a_governed_bound():
    # Review MED #3: an all-None cap with no scope budget is NOT a governed launch — refuse it.
    assert is_enforceable(AgentCellCap(), []) is False
    assert is_enforceable(AgentCellCap(cost_usd=2.0), []) is True
    assert is_enforceable(AgentCellCap(seconds=300.0), []) is True
    assert is_enforceable(AgentCellCap(max_steps=5), []) is True
    # A real scope budget also makes it enforceable even with an all-None cell cap.
    assert is_enforceable(AgentCellCap(), [("tenant", 0.0, 50.0)]) is True
    # A non-finite/zero cost cap does NOT count as a bound.
    assert is_enforceable(AgentCellCap(cost_usd=float("nan")), []) is False
    assert is_enforceable(AgentCellCap(cost_usd=0.0), []) is False


def test_non_finite_time_cap_fails_closed():
    # Review LOW #4: a NaN deadline (or elapsed) is treated as reached — no step starts.
    assert deadline_reached(AgentCellCap(seconds=float("nan")), 1.0) is True
    assert deadline_reached(AgentCellCap(seconds=300.0), float("nan")) is True
