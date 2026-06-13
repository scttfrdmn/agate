"""Unit tests for Cedar policy generation (§8, §13.4). No AWS."""

from __future__ import annotations

from agg.entitlements import TIERS, models_for_tier
from policy.cedar import (
    call_tool_policy,
    forbid_cross_tenant,
    generate_policy_set,
    model_invoke_policies,
    retrieve_policy,
)


def test_model_policies_cover_every_tier():
    text = model_invoke_policies()
    for tier in TIERS:
        assert f'principal.tier == "{tier}"' in text
    # one permit block per tier
    assert text.count('action == Action::"InvokeModel"') == len(TIERS)


def test_model_policies_mirror_the_entitlement_table():
    # The Cedar model set must equal the IAM/entitlement table set (one source).
    text = model_invoke_policies()
    for m in models_for_tier("frontier"):
        assert f'"{m}"' in text
    # cumulative: an oss model appears in the frontier permit's resource list
    assert f'"{models_for_tier("oss")[0]}"' in text


def test_model_policy_ties_tier_and_tenant():
    text = model_invoke_policies()
    assert "resource.tier == principal.tier" in text
    assert "resource.tenant == principal.tenant" in text


def test_retrieve_policy_scopes_to_tenant_and_courses():
    text = retrieve_policy()
    assert "resource.index_tenant == principal.tenant" in text
    assert "principal.courses" in text
    assert 'action == Action::"Retrieve"' in text


def test_call_tool_policy_gates_tool_and_tenant():
    text = call_tool_policy()
    assert "resource.tool in principal.allowed_tools" in text
    assert "resource.tenant == principal.tenant" in text


def test_forbid_cross_tenant_is_a_forbid():
    text = forbid_cross_tenant()
    assert text.lstrip().startswith("// ") or "forbid(" in text
    assert "forbid(principal, action, resource)" in text
    assert "resource.tenant != principal.tenant" in text


def test_full_policy_set_includes_all_parts():
    text = generate_policy_set()
    for needle in [
        'action == Action::"InvokeModel"',
        'action == Action::"Retrieve"',
        'action == Action::"CallTool"',
        "forbid(principal, action, resource)",
    ]:
        assert needle in text


def test_policy_set_is_nonempty_and_commented():
    text = generate_policy_set()
    assert len(text) > 200
    assert "//" in text  # human-auditable comments present
