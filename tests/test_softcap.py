"""Unit tests for the soft-cap decision (§7.1). No AWS."""

from __future__ import annotations

from cost import evaluate_soft_cap


def test_no_budget_always_allows():
    r = evaluate_soft_cap(spend=999.0, budget=None)
    assert r.decision == "allow"
    assert r.utilisation == 0.0


def test_within_budget_allows():
    r = evaluate_soft_cap(spend=2.0, budget=10.0)
    assert r.decision == "allow"
    assert r.utilisation == 0.2
    assert r.reason == "within budget"


def test_approaching_budget_warns_but_allows():
    r = evaluate_soft_cap(spend=9.5, budget=10.0)
    assert r.decision == "allow"
    assert r.reason == "approaching budget"


def test_over_budget_denies():
    r = evaluate_soft_cap(spend=10.0, budget=10.0)
    assert r.decision == "deny"
    assert r.reason == "over budget"
    assert evaluate_soft_cap(spend=11.0, budget=10.0).decision == "deny"


def test_zero_or_negative_budget_denies():
    assert evaluate_soft_cap(spend=0.0, budget=0.0).decision == "deny"
    assert evaluate_soft_cap(spend=0.0, budget=-5.0).decision == "deny"


def test_invalid_negative_spend_fails_closed():
    # A negative authoritative spend must never widen access.
    r = evaluate_soft_cap(spend=-1.0, budget=10.0)
    assert r.decision == "deny"
    assert r.reason == "invalid spend"


def test_custom_warn_threshold():
    # warn at 50% utilisation
    assert evaluate_soft_cap(spend=6.0, budget=10.0, warn_at=0.5).reason == "approaching budget"
    assert evaluate_soft_cap(spend=4.0, budget=10.0, warn_at=0.5).reason == "within budget"
