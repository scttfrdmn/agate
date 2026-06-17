"""Unit tests for Cedar policy generation (§8, §13.4). No AWS."""

from __future__ import annotations

import pytest
from agate.entitlements import TIERS, models_for_tier
from policy.cedar import (
    AGENTCORE_GATEWAY_TYPE,
    agentcore_tool_policy_statements,
    call_tool_policy,
    forbid_cross_tenant,
    generate_policy_set,
    model_invoke_policies,
    policy_statements,
    retrieve_policy,
)

_GW_ARN = "arn:aws:bedrock-agentcore:us-east-1:942542972736:gateway/agate-demo-xsrgzb8b6f"


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


def test_policy_statements_are_separate_single_statements():
    # AgentCore CfnPolicy holds ONE statement each, so the set is split per-statement: a
    # tier permit each + retrieve + call-tool + the forbid. None may contain two statements.
    stmts = policy_statements()
    names = [n for n, _ in stmts]
    assert names == [
        *(f"invoke-{t}" for t in TIERS),
        "retrieve",
        "call-tool",
        "forbid-cross-tenant",
    ]
    for name, body in stmts:
        # exactly one Cedar statement -> exactly one terminating `;`
        assert body.count(";") == 1, f"{name} must be a single statement"
        assert body.rstrip().endswith(";")
    # the forbid is its own statement (the bug was it trailing a permit in one string)
    forbid = dict(stmts)["forbid-cross-tenant"]
    assert forbid.lstrip().startswith("//") and "forbid(" in forbid
    assert "permit(" not in forbid


# --- agent-path AgentCore tool policy (#154) ------------------------------- #
def test_agentcore_tool_policy_one_statement_per_tool():
    stmts = agentcore_tool_policy_statements(_GW_ARN, ["hpc-submit", "hpc-monitor"], "agate-slurm")
    assert [n for n, _ in stmts] == ["tool-hpc-submit", "tool-hpc-monitor"]
    for _, body in stmts:
        # exactly one Cedar statement -> one terminating `;`
        assert body.count(";") == 1
        assert body.rstrip().endswith(";")


def test_agentcore_tool_policy_uses_agentcore_schema_not_abstract_resource():
    # The #154 fix: AgentCore rejects an abstract `resource` — every statement must pin the
    # specific AgentCore::Gateway ARN, a tool action, and an authenticated principal type.
    _, body = agentcore_tool_policy_statements(_GW_ARN, ["hpc-submit"], "agate-slurm")[0]
    assert f'{AGENTCORE_GATEWAY_TYPE}::"{_GW_ARN}"' in body
    assert 'action == AgentCore::Action::"agate-slurm___hpc-submit"' in body
    assert "principal is AgentCore::IamEntity" in body
    # NOT the abstract chat-path mirror shapes
    assert 'Action::"InvokeModel"' not in body
    assert "resource.tier" not in body


def test_agentcore_tool_policy_carries_a_constraining_when():
    # A bare permit fails the analyzer ("Overly Permissive", confirmed live) — every statement
    # must carry a constraining `when` on an identified principal.
    _, body = agentcore_tool_policy_statements(_GW_ARN, ["hpc-submit"], "agate-slurm")[0]
    assert "when {" in body
    assert "principal has id" in body and 'principal.id != ""' in body


def test_agentcore_tool_policy_rejects_unsupported_principal_type():
    # AgentCore::UnauthenticatedUser is not a valid principal type in a policy (confirmed live).
    with pytest.raises(ValueError, match="principal type"):
        agentcore_tool_policy_statements(
            _GW_ARN, ["hpc-submit"], "agate-slurm", principal_type="AgentCore::UnauthenticatedUser"
        )


def test_agentcore_tool_policy_empty_tool_list_is_empty():
    assert agentcore_tool_policy_statements(_GW_ARN, [], "agate-slurm") == []
