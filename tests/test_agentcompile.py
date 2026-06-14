"""Unit tests for the agent-spec compiler (#105). No AWS — pure policy/tag/payload shape."""

from __future__ import annotations

from agate.agentcompile import CompiledAgent, compile_agent
from agate.agentspec import parse_spec
from agate.budget import _scope_pk, _tenant_pk, _user_pk
from agate.entitlements import models_for_tier
from agate.patterns import compile_pattern


def _spec(**over):
    d = {
        "agent": "chem101-ta",
        "description": "Drafts feedback for instructor review.",
        "role": "ta",
        "scope": "chemistry/chem-101",
        "reasoning": "lit-review",
        "tools": ["course-materials-reader"],
    }
    d.update(over)
    return parse_spec(d)


def _sids(doc):
    return [s["Sid"] for s in doc["Statement"]]


def test_compiles_to_compiled_agent():
    c = compile_agent(_spec(), region="us-east-1", account="123")
    assert isinstance(c, CompiledAgent)


def test_tags_template_matches_role_and_scope():
    c = compile_agent(_spec())
    assert c.tags_template.tier == "oss"  # ta -> oss
    assert c.tags_template.scope == "chemistry/chem-101"
    # tenant is a placeholder filled at spawn (#106), not a real value
    assert c.tags_template.tenant.startswith("{")


def test_tool_policy_has_one_allow_per_declared_tool_and_none_for_undeclared():
    c = compile_agent(_spec(tools=["course-materials-reader", "gradebook-drafts"]))
    sids = _sids(c.tool_policy)
    assert "ToolCourseMaterialsReader" in sids
    assert "ToolGradebookDrafts" in sids
    # the fail-closed deny is always present
    assert "DenyToolsWhenNoTenantTag" in sids
    # exactly 2 Allows (+ 1 Deny) for 2 declared tools
    allows = [s for s in c.tool_policy["Statement"] if s["Effect"] == "Allow"]
    assert len(allows) == 2


def test_no_tools_yields_deny_only_policy():
    c = compile_agent(_spec(tools=[]))
    allows = [s for s in c.tool_policy["Statement"] if s["Effect"] == "Allow"]
    assert allows == []
    assert "DenyToolsWhenNoTenantTag" in _sids(c.tool_policy)


def test_tool_resource_is_tenant_and_scope_fenced():
    c = compile_agent(_spec(tools=["course-materials-reader"]))
    allow = next(s for s in c.tool_policy["Statement"] if s["Sid"] == "ToolCourseMaterialsReader")
    res = allow["Resource"][0]
    assert "${aws:PrincipalTag/agate:tenant}" in res
    assert "${aws:PrincipalTag/agate:scope}" in res  # confined to the subtree


def test_write_tool_targets_a_draft_path():
    c = compile_agent(_spec(tools=["gradebook-drafts"]))
    allow = next(s for s in c.tool_policy["Statement"] if s["Sid"] == "ToolGradebookDrafts")
    assert "_drafts/" in allow["Resource"][0]  # never a live system (vision §5)


def test_dispatch_payload_equals_compile_pattern_composition():
    # The compiler COMPOSES compile_pattern, it doesn't reimplement it.
    spec = _spec()
    c = compile_agent(spec, question="What does the evidence say?")
    expected = compile_pattern(
        spec.reasoning,
        question="What does the evidence say?",
        entitled_models=models_for_tier(spec.tier),
    )
    assert c.dispatch_payload == expected


def test_reasoning_models_never_exceed_tier():
    # An oss-tier agent's resolved roster only names oss models.
    spec = _spec(role="ta")  # oss
    c = compile_agent(spec, question="x")
    entitled = set(models_for_tier("oss"))
    assert all(m["tier"] in entitled for m in c.dispatch_payload["roster"])


# --- budget row templates use the exact cascade key shapes (#81) ------------


def test_scope_budget_row_uses_scope_key_shape():
    c = compile_agent(_spec(budget={"usd": 100, "per": "scope", "period": "term"}))
    row = c.budget_rows[0]
    assert row.pk == _scope_pk("{tenant}", "chemistry/chem-101", "{period}")
    assert row.budget_usd == 100.0


def test_student_budget_row_uses_user_key_shape():
    c = compile_agent(_spec(budget="$20 / student / term"))
    assert c.budget_rows[0].pk == _user_pk("{tenant}", "{user}", "{period}")


def test_tenant_budget_row_uses_tenant_key_shape():
    c = compile_agent(_spec(budget={"usd": 500, "per": "tenant", "period": "month"}))
    assert c.budget_rows[0].pk == _tenant_pk("{tenant}", "{period}")


def test_no_budget_yields_no_rows():
    assert compile_agent(_spec()).budget_rows == ()


# --- model-access policy is the generated, tier-gated one -------------------


def test_model_access_policy_is_tier_gated():
    c = compile_agent(_spec(), region="us-east-1")
    sids = _sids(c.model_access_policy)
    assert {"InvokeTierOss", "InvokeTierMid", "InvokeTierFrontier"} == set(sids)


def test_triggers_are_shape_only_descriptors():
    c = compile_agent(_spec(triggers=[{"on": "lms:submitted", "then": "draft"}]))
    assert c.triggers == ({"on": "lms:submitted", "then": "draft"},)
