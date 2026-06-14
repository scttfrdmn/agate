"""Unit tests for the pure budget-authoring core (#87). No AWS."""

from __future__ import annotations

import pytest
from agate.budget import (
    BudgetError,
    is_within_admin_scope,
    normalise_scope,
    plan_budget_write,
)

# --- key PARITY with the reader (meter.parse) -------------------------------
# The whole point: the writer's keys must equal the chokepoint's read keys, or it
# writes rows the cascade never reads. agate can't import meter (cycle), so assert
# equality here instead.


def test_scope_key_parity_with_meter():
    from agate.budget import _scope_pk
    from meter import scope_pk

    assert _scope_pk("chem", "chemistry/chem-101", "2026-06") == scope_pk(
        "chem", "chemistry/chem-101", "2026-06"
    )


def test_user_and_tenant_key_parity_with_meter():
    from agate.budget import _tenant_pk, _user_pk
    from meter import spend_key, spend_rollup_key

    assert _user_pk("chem", "student-7", "2026-06") == spend_key("chem", "student-7", "2026-06")
    assert _tenant_pk("chem", "2026-06") == spend_rollup_key("chem", "2026-06")


def test_planned_scope_write_matches_chokepoint_read_key():
    # End-to-end: the pk a tenant-wide admin plans is exactly what lookup_scope_budget reads.
    from meter import scope_pk

    w = plan_budget_write(
        actor_tenant="chem",
        actor_admin_scope=(),
        tenant="chem",
        usd=100.0,
        period="2026-06",
        scope="chemistry/chem-101",
    )
    assert w.pk == scope_pk("chem", "chemistry/chem-101", "2026-06")


# --- scope normalisation matches the tags grammar ---------------------------


def test_normalise_scope_matches_tags_for_single_value():
    from agate.tags import _normalise_data_scope

    for raw in ("chemistry", "/chemistry/chem-101/", "arts-sci/chem", "bad chars!@#/ok"):
        assert normalise_scope(raw) == _normalise_data_scope(raw)


def test_normalise_scope_empty():
    assert normalise_scope("") == ""
    assert normalise_scope("///") == ""


# --- subtree containment (segment-wise, not string-prefix) ------------------


def test_tenant_wide_admin_governs_any_node():
    assert is_within_admin_scope("anything/at-all", ()) is True
    assert is_within_admin_scope("", ()) is True


def test_scoped_admin_governs_own_subtree_and_descendants():
    scope = ("chemistry",)
    assert is_within_admin_scope("chemistry", scope) is True
    assert is_within_admin_scope("chemistry/chem-101", scope) is True


def test_scoped_admin_blocked_from_siblings_and_root():
    scope = ("chemistry",)
    assert is_within_admin_scope("physics", scope) is False
    assert is_within_admin_scope("", scope) is False  # tenant root
    # string-prefix trap: "chem" must NOT contain "chemistry"
    assert is_within_admin_scope("chemistry", ("chem",)) is False


# --- plan_budget_write: validation + authorization --------------------------


def test_tenant_wide_admin_sets_tenant_budget():
    w = plan_budget_write(
        actor_tenant="chem", actor_admin_scope=(), tenant="chem", usd=500, period="2026-06"
    )
    assert w.pk == "chem#2026-06"
    assert w.budget_usd == 500.0 and w.scope == "" and w.user == ""


def test_tenant_wide_admin_sets_user_budget():
    w = plan_budget_write(
        actor_tenant="chem",
        actor_admin_scope=(),
        tenant="chem",
        usd=20,
        period="2026-06",
        user="student-7",
    )
    assert w.pk == "chem#student-7#2026-06"
    assert w.user == "student-7"


def test_cross_tenant_write_is_rejected():
    with pytest.raises(BudgetError, match="another tenant"):
        plan_budget_write(
            actor_tenant="chem", actor_admin_scope=(), tenant="physics", usd=1, period="2026-06"
        )


def test_scoped_admin_cannot_set_tenant_budget():
    with pytest.raises(BudgetError, match="scoped admin"):
        plan_budget_write(
            actor_tenant="chem",
            actor_admin_scope=("chemistry",),
            tenant="chem",
            usd=1,
            period="2026-06",
        )


def test_scoped_admin_cannot_set_user_budget():
    with pytest.raises(BudgetError, match="scoped admin"):
        plan_budget_write(
            actor_tenant="chem",
            actor_admin_scope=("chemistry",),
            tenant="chem",
            usd=1,
            period="2026-06",
            user="student-7",
        )


def test_scoped_admin_sets_own_subtree_budget():
    w = plan_budget_write(
        actor_tenant="chem",
        actor_admin_scope=("chemistry",),
        tenant="chem",
        usd=250,
        period="2026-06",
        scope="chemistry/chem-101",
    )
    assert w.pk == "chem#scope#chemistry/chem-101#2026-06"
    assert w.scope == "chemistry/chem-101"


def test_scoped_admin_blocked_from_sibling_subtree():
    with pytest.raises(BudgetError, match="outside your administrative subtree"):
        plan_budget_write(
            actor_tenant="chem",
            actor_admin_scope=("chemistry",),
            tenant="chem",
            usd=1,
            period="2026-06",
            scope="physics/phys-101",
        )


def test_negative_budget_rejected():
    with pytest.raises(BudgetError, match=">= 0"):
        plan_budget_write(
            actor_tenant="chem", actor_admin_scope=(), tenant="chem", usd=-5, period="2026-06"
        )


def test_bad_period_rejected():
    with pytest.raises(BudgetError, match="YYYY-MM"):
        plan_budget_write(
            actor_tenant="chem", actor_admin_scope=(), tenant="chem", usd=1, period="2026"
        )


def test_bool_usd_rejected():
    # bool is a subclass of int — must not sneak through as 0/1 dollars.
    with pytest.raises(BudgetError, match="must be a number"):
        plan_budget_write(
            actor_tenant="chem", actor_admin_scope=(), tenant="chem", usd=True, period="2026-06"
        )


def test_scope_and_user_both_rejected():
    with pytest.raises(BudgetError, match="not both"):
        plan_budget_write(
            actor_tenant="chem",
            actor_admin_scope=(),
            tenant="chem",
            usd=1,
            period="2026-06",
            scope="chemistry",
            user="student-7",
        )


def test_scope_that_normalises_empty_rejected():
    with pytest.raises(BudgetError, match="valid path"):
        plan_budget_write(
            actor_tenant="chem",
            actor_admin_scope=(),
            tenant="chem",
            usd=1,
            period="2026-06",
            scope="///",
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_budget_rejected(bad):
    # SECURITY (#87 review): a NaN budget would pass `usd < 0` (nan<0 is False) AND the
    # chokepoint's `spend > budget` (always False) -> enforcement silently disabled.
    # NaN/inf must be rejected before the range check. Fail closed.
    with pytest.raises(BudgetError, match="finite"):
        plan_budget_write(
            actor_tenant="chem", actor_admin_scope=(), tenant="chem", usd=bad, period="2026-06"
        )


def test_scope_path_traversal_rejected():
    # A `..` segment must not produce a reachable/garbage key.
    assert normalise_scope("chemistry/../physics") == ""
    with pytest.raises(BudgetError, match="valid path"):
        plan_budget_write(
            actor_tenant="chem",
            actor_admin_scope=("chemistry",),
            tenant="chem",
            usd=1,
            period="2026-06",
            scope="chemistry/../physics",
        )
