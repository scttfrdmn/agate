"""Unit tests for agent payments — the governance core (#120). No AWS.

The §8.6/§10 invariant: a payment is another metered action under the SAME single boundary.
A Mandate is the compiled `budget` made scoped + delegatable; a child's spending authority is
provably ⊆ its parent's; every priced call is pre-checked against the budget ceiling (never
the vendor's quoted price) and attributed per-hop.
"""

from __future__ import annotations

import pytest
from agate.agentcompile import compile_agent
from agate.agentspec import BudgetSpec, parse_spec
from agate.identity import acting_as_from_session
from agate.payments import (
    PaymentError,
    authorize_spend,
    delegate_mandate,
    mandate_from_budget,
    priced_action_row,
)
from agate.tags import role_session_name


def _agent(scope="lab/photonics", budget="$50 / user / month"):
    d = {
        "agent": "researcher", "description": "d", "role": "researcher",
        "scope": scope, "reasoning": "lit-review",
    }
    if budget is not None:
        d["budget"] = budget
    return compile_agent(parse_spec(d))


def _caa(subject="sub", tenant="uni"):
    return acting_as_from_session(role_session_name(tenant, subject), agent=f"{tenant}/{subject}")


# --- mandate from budget -----------------------------------------------------


def test_mandate_from_budget_carries_limit_scope_and_attribution():
    m = mandate_from_budget(_agent(), tenant="uni", subject="prof", scope="lab/photonics")
    assert m.limit_usd == 50.0
    assert m.scope == "lab/photonics"
    assert m.period == "month"
    assert m.acting_as.on_behalf_of == "uni@prof"
    assert m.acting_as.attributed is True


def test_no_budget_means_no_mandate():
    # No declared budget = no spending authority.
    assert mandate_from_budget(_agent(budget=None), tenant="uni", subject="prof") is None


def test_mandate_scope_defaults_to_agent_scope():
    m = mandate_from_budget(_agent(scope="lab/optics"), tenant="uni", subject="prof")
    assert m.scope == "lab/optics"


# --- the headline: delegation only narrows ----------------------------------


def test_child_mandate_capped_to_parent_remaining():
    parent = mandate_from_budget(_agent(), tenant="uni", subject="prof")  # $50
    # child asks for MORE than the parent -> capped to the parent's ceiling
    child = delegate_mandate(
        parent, BudgetSpec(usd=80.0, per="user", period_kind="month"),
        child_subject="sub", child_scope="lab/photonics", child_acting_as=_caa(),
    )
    assert child.limit_usd == 50.0


def test_child_mandate_respects_its_smaller_ask():
    parent = mandate_from_budget(_agent(), tenant="uni", subject="prof")  # $50
    child = delegate_mandate(
        parent, BudgetSpec(usd=20.0, per="user", period_kind="month"),
        child_subject="sub", child_scope="lab/photonics/sub", child_acting_as=_caa(),
    )
    assert child.limit_usd == 20.0
    assert child.scope == "lab/photonics/sub"  # narrowed to the deeper scope


def test_disjoint_child_scope_refused():
    parent = mandate_from_budget(_agent(), tenant="uni", subject="prof")
    with pytest.raises(PaymentError):
        delegate_mandate(
            parent, None, child_subject="sub", child_scope="physics", child_acting_as=_caa(),
        )


def test_delegation_is_monotonic_over_two_hops():
    parent = mandate_from_budget(_agent(), tenant="uni", subject="prof")  # $50
    child = delegate_mandate(
        parent, BudgetSpec(usd=30.0, per="user", period_kind="month"),
        child_subject="c", child_scope="lab/photonics", child_acting_as=_caa("c"),
    )
    grandchild = delegate_mandate(
        child, BudgetSpec(usd=40.0, per="user", period_kind="month"),  # asks MORE than child
        child_subject="g", child_scope="lab/photonics", child_acting_as=_caa("g"),
    )
    # grandchild ⊆ child ⊆ parent: 40 asked, but child only has 30, parent only 50
    assert grandchild.limit_usd == 30.0
    assert child.limit_usd == 30.0
    assert grandchild.limit_usd <= child.limit_usd <= parent.limit_usd


# --- authorize spend: the gate bites, ceiling is authority ------------------


def test_spend_within_budget_allowed():
    m = mandate_from_budget(_agent(), tenant="uni", subject="prof")
    assert authorize_spend(m, price_usd=10.0, spend_so_far=0.0).decision == "allow"


def test_spend_over_budget_rejected():
    m = mandate_from_budget(_agent(), tenant="uni", subject="prof")  # $50
    res = authorize_spend(m, price_usd=10.0, spend_so_far=45.0)  # 55 > 50
    assert res.decision == "reject"
    assert res.projected_total == 55.0


def test_negative_price_fails_closed():
    m = mandate_from_budget(_agent(), tenant="uni", subject="prof")
    assert authorize_spend(m, price_usd=-1.0, spend_so_far=0.0).decision == "reject"


def test_non_finite_price_cannot_bypass_the_gate():
    # CRITICAL (security review): a NaN price compares False to every budget, so without a
    # guard the money gate fails OPEN. NaN/inf must be rejected even when spend is already
    # way over budget.
    m = mandate_from_budget(_agent(), tenant="uni", subject="prof")  # $50
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert authorize_spend(m, price_usd=bad, spend_so_far=100.0).decision == "reject"


def test_ceiling_not_price_is_the_authority():
    # Even a huge single price is fine if it fits; even a tiny one is rejected if over.
    m = mandate_from_budget(_agent(), tenant="uni", subject="prof")  # $50
    assert authorize_spend(m, price_usd=49.99, spend_so_far=0.0).decision == "allow"
    assert authorize_spend(m, price_usd=0.01, spend_so_far=50.0).decision == "reject"


# --- attribution -------------------------------------------------------------


def test_priced_action_row_is_attributed():
    m = mandate_from_budget(_agent(), tenant="uni", subject="prof")
    row = priced_action_row(m, price_usd=0.05, label="dataset-fetch", vendor="arxiv")
    assert row["kind"] == "x402"
    assert row["cost"] == 0.05
    assert row["vendor"] == "arxiv"
    assert row["actingAs"]["on_behalf_of"] == "uni@prof"
