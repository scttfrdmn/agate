"""Unit tests for the effective-boundary view (#108). No AWS — pure."""

from __future__ import annotations

from agate.agentcompile import compile_agent
from agate.agentspec import parse_spec
from agate.boundary import EffectiveBoundary, describe, describe_instantiated
from agate.delegate import instantiate_for_invoker
from agate.tags import SessionTags


def _spec(**over):
    d = {
        "agent": "chem101-ta",
        "description": "d",
        "role": "ta",
        "scope": "chemistry/chem-101",
        "reasoning": "lit-review",
        "tools": ["course-materials-reader", "gradebook-drafts"],
        "budget": "$20 / student / term",
    }
    d.update(over)
    return parse_spec(d)


def _boundary(**over) -> EffectiveBoundary:
    return describe(compile_agent(_spec(**over)))


def _text(b: EffectiveBoundary) -> str:
    return "\n".join(b.summary())


# --- models -----------------------------------------------------------------


def test_lists_entitled_models_and_denies_higher_tier():
    t = _text(_boundary())  # ta -> oss
    assert "oss-tier models" in t
    assert "openai.gpt-oss-20b-1:0" in t
    assert "higher-tier models (mid, frontier)" in t  # the explicit denial


def test_frontier_agent_has_no_higher_tier_denial():
    b = _boundary(role="researcher")  # frontier
    assert b.tier == "frontier"
    assert not any(i.kind == "model" and not i.allow for i in b.denials)


# --- data scope -------------------------------------------------------------


def test_scoped_agent_confined_to_subtree_with_denial():
    t = _text(_boundary())
    assert "{tenant}/chemistry/chem-101/ only" in t
    assert "outside {tenant}/chemistry/chem-101/" in t


def test_tenant_wide_agent_says_tenant_wide():
    b = parse_spec({"agent": "a", "description": "d", "role": "ta", "reasoning": "lit-review"})
    t = _text(describe(compile_agent(b)))
    assert "tenant-wide" in t
    assert "another tenant's documents" in t  # still denies cross-tenant


# --- tools ------------------------------------------------------------------


def test_tools_named_with_read_vs_draft_write():
    t = _text(_boundary())
    assert "read-only" in t
    assert "draft queue, never live" in t  # the write tool is draft-only
    assert "any tool not listed above (denied by absence)" in t


def test_no_tools_still_denies_undeclared():
    b = _boundary(tools=[])
    assert not any(i.kind == "tool" and i.allow for i in b.allows)
    assert any("denied by absence" in i.detail for i in b.denials)


# --- spend ------------------------------------------------------------------


def test_spend_ceiling_rendered():
    assert "spend up to $20 per student per term" in _text(_boundary())


def test_no_budget_says_no_ceiling():
    d = {
        "agent": "a",
        "description": "d",
        "role": "ta",
        "scope": "chemistry/chem-101",
        "reasoning": "lit-review",
    }
    t = _text(describe(compile_agent(parse_spec(d))))
    assert "no budget ceiling declared" in t


# --- per-invoker variant + serialisation ------------------------------------


def test_describe_instantiated_names_invoker_and_narrowed_scope():
    spec = parse_spec(
        {
            "agent": "chem-ta",
            "description": "d",
            "role": "ta",
            "scope": "chemistry",
            "reasoning": "lit-review",
            "invokers": "scope:chemistry",
        }
    )
    alice = SessionTags(
        affiliation="student",
        tenant="chem",
        courses=("chem-101",),
        tier="oss",
        scope="chemistry/chem-101",
    )
    inst = instantiate_for_invoker(alice, spec, subject="alice")
    b = describe_instantiated(inst)
    assert b.subject == "alice"
    t = _text(b)
    assert "(for alice)" in t
    assert "{tenant}/chemistry/chem-101/ only" in t  # the NARROWED child scope


def test_to_dict_round_trips():
    b = _boundary()
    d = b.to_dict()
    assert d["agent_name"] == "chem101-ta"
    assert d["tier"] == "oss"
    assert d["scope"] == "chemistry/chem-101"
    assert isinstance(d["allows"], list) and isinstance(d["denials"], list)
    assert all({"kind", "allow", "detail"} == set(i) for i in d["allows"] + d["denials"])
