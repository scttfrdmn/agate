"""Unit tests for the hierarchical budget cascade gate (#81, §7.1). No AWS."""

from __future__ import annotations

from cost import estimate_call_cost, evaluate_cascade
from cost.pricing import ModelRate, PriceBook

PB = PriceBook(model_rates={"frontier": ModelRate(input_per_mtok=3.0, output_per_mtok=15.0)})


def _cascade(nodes, *, input_tokens=1000, max_tokens=1000):
    return evaluate_cascade(
        model_id="frontier",
        input_tokens=input_tokens,
        max_tokens=max_tokens,
        nodes=nodes,
        pricebook=PB,
    )


def test_all_nodes_within_budget_allows():
    r = _cascade([("user", 1.0, 100.0), ("scope:chem", 2.0, 50.0)])
    assert r.decision == "allow"
    assert r.breaching_node is None


def test_single_node_over_rejects_and_names_it():
    r = _cascade([("user", 100.0, 100.0)])  # spend already at budget
    assert r.decision == "reject"
    assert r.breaching_node == "user"
    assert r.reason == "would exceed budget"


def test_first_breaching_ancestor_is_named():
    # user + school OK, but the DEPT node is exhausted -> reject names the dept.
    nodes = [
        ("user", 0.0, 100.0),
        ("scope:arts-sci", 0.0, 100.0),
        ("scope:arts-sci/chemistry", 99.999, 100.0),  # dept nearly exhausted
        ("scope:arts-sci/chemistry/chem-101", 0.0, 100.0),
    ]
    r = _cascade(nodes)
    assert r.decision == "reject"
    assert r.breaching_node == "scope:arts-sci/chemistry"


def test_none_budget_node_is_skipped():
    # A node with no budget row imposes no cap; others still gate.
    r = _cascade([("user", 0.0, None), ("scope:chem", 0.0, 100.0)])
    assert r.decision == "allow"


def test_zero_budget_rejects_no_allocation():
    r = _cascade([("scope:chem", 0.0, 0.0)])
    assert r.decision == "reject"
    assert r.reason == "no allocation"
    assert r.breaching_node == "scope:chem"


def test_negative_spend_fails_closed():
    r = _cascade([("user", -1.0, 100.0)])
    assert r.decision == "reject"
    assert r.reason == "invalid spend"


def test_empty_nodes_allows():
    # An unconfined session with no caps -> nothing to check -> allow.
    r = _cascade([])
    assert r.decision == "allow"
    assert r.breaching_node is None


def test_estimate_priced_once_independent_of_node_count():
    expected = estimate_call_cost("frontier", 1000, 1000, pricebook=PB)
    one = _cascade([("user", 0.0, 100.0)])
    many = _cascade([("user", 0.0, 100.0), ("a", 0.0, 100.0), ("b", 0.0, 100.0)])
    assert one.estimated_cost == expected
    assert many.estimated_cost == expected
