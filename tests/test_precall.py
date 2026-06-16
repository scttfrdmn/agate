"""Unit tests for the exact pre-call budget gate (§7.1, Tier 1). No AWS."""

from __future__ import annotations

import pytest
from cost import estimate_call_cost, evaluate_precall
from cost.pricing import ModelRate, PriceBook

PB = PriceBook(model_rates={"frontier": ModelRate(input_per_mtok=3.0, output_per_mtok=15.0)})


def test_estimate_is_worst_case():
    # 1M input @ $3 + 1000 max_tokens @ $15/M = 3.0 + 0.015 = 3.015
    est = estimate_call_cost("frontier", 1_000_000, 1000, pricebook=PB)
    assert est == pytest.approx(3.015)


def test_allows_when_projection_within_budget():
    r = evaluate_precall(
        model_id="frontier",
        input_tokens=1000,
        max_tokens=1000,
        spend=1.0,
        budget=100.0,
        pricebook=PB,
    )
    assert r.decision == "allow"
    assert r.projected_total == pytest.approx(1.0 + r.estimated_cost)


def test_rejects_when_projection_exceeds_budget():
    # spend already at 99.99, a call that could cost more than 0.01 -> reject
    r = evaluate_precall(
        model_id="frontier",
        input_tokens=1_000_000,
        max_tokens=1000,
        spend=99.99,
        budget=100.0,
        pricebook=PB,
    )
    assert r.decision == "reject"
    assert r.reason == "would exceed budget"


def test_no_budget_allows():
    r = evaluate_precall(
        model_id="frontier", input_tokens=10, max_tokens=10, spend=5.0, budget=None, pricebook=PB
    )
    assert r.decision == "allow"


def test_zero_budget_rejects():
    r = evaluate_precall(
        model_id="frontier", input_tokens=1, max_tokens=1, spend=0.0, budget=0.0, pricebook=PB
    )
    assert r.decision == "reject"
    assert r.reason == "no allocation"


def test_negative_spend_fails_closed():
    r = evaluate_precall(
        model_id="frontier", input_tokens=1, max_tokens=1, spend=-1.0, budget=100.0, pricebook=PB
    )
    assert r.decision == "reject"
    assert r.reason == "invalid spend"


def test_exact_boundary_allows_when_equal():
    # projected exactly equal to budget is allowed (only strictly-over rejects).
    est = estimate_call_cost("frontier", 0, 1000, pricebook=PB)  # 0.015
    r = evaluate_precall(
        model_id="frontier",
        input_tokens=0,
        max_tokens=1000,
        spend=round(10.0 - est, 6),
        budget=10.0,
        pricebook=PB,
    )
    assert r.decision == "allow"


def test_precall_is_stricter_than_soft_cap():
    # Under budget now (soft cap would allow), but the next call's worst case
    # would exceed -> pre-call rejects. This is the Tier 1 difference.
    from cost import evaluate_soft_cap

    spend, budget = 9.99, 10.0
    assert evaluate_soft_cap(spend, budget).decision == "allow"
    pre = evaluate_precall(
        model_id="frontier",
        input_tokens=1_000_000,
        max_tokens=1000,
        spend=spend,
        budget=budget,
        pricebook=PB,
    )
    assert pre.decision == "reject"


# --- flat-USD priced-action gates (#120) ------------------------------------


def test_priced_call_allows_within_budget():
    from cost import evaluate_priced_call

    r = evaluate_priced_call(price_usd=0.05, spend=1.0, budget=10.0)
    assert r.decision == "allow"
    assert r.estimated_cost == 0.05
    assert r.projected_total == 1.05


def test_priced_call_rejects_over_budget():
    from cost import evaluate_priced_call

    r = evaluate_priced_call(price_usd=5.0, spend=8.0, budget=10.0)  # 13 > 10
    assert r.decision == "reject"


def test_priced_call_no_budget_allows_and_negative_price_fails_closed():
    from cost import evaluate_priced_call

    assert evaluate_priced_call(price_usd=99.0, spend=0.0, budget=None).decision == "allow"
    assert evaluate_priced_call(price_usd=-1.0, spend=0.0, budget=10.0).decision == "reject"


def test_priced_cascade_names_first_breaching_node():
    from cost import evaluate_priced_cascade

    nodes = [("tenant", 0.0, 100.0), ("lab/photonics", 9.99, 10.0)]
    r = evaluate_priced_cascade(price_usd=0.5, nodes=nodes)  # fits tenant, breaches lab
    assert r.decision == "reject"
    assert r.breaching_node == "lab/photonics"


def test_priced_cascade_empty_nodes_and_none_budget_allow():
    from cost import evaluate_priced_cascade

    assert evaluate_priced_cascade(price_usd=1.0, nodes=[]).decision == "allow"
    assert (
        evaluate_priced_cascade(price_usd=1.0, nodes=[("t", 0.0, None)]).decision == "allow"
    )


def test_non_finite_price_fails_closed_in_both_priced_gates():
    # A NaN price compares False to every budget — without a guard the gate fails OPEN.
    from cost import evaluate_priced_call, evaluate_priced_cascade

    for bad in (float("nan"), float("inf"), float("-inf")):
        assert evaluate_priced_call(price_usd=bad, spend=0.0, budget=10.0).decision == "reject"
        assert (
            evaluate_priced_cascade(price_usd=bad, nodes=[("n", 0.0, 10.0)]).decision
            == "reject"
        )


def test_node_decision_rejects_non_finite_spend():
    # The shared guard also rejects a non-finite SPEND (a corrupt spend-table read), so no
    # gate (token or priced) can be bypassed by NaN on either side.
    from cost import evaluate_priced_cascade

    r = evaluate_priced_cascade(price_usd=1.0, nodes=[("n", float("nan"), 10.0)])
    assert r.decision == "reject"
