"""Unit tests for the generated IAM policy documents. No AWS — pure JSON shape."""

from __future__ import annotations

from agate.entitlements import TIER_MODELS, foundation_model_arn
from policy.generate import (
    agent_write_policy,
    data_scope_policy,
    model_access_policy,
    vector_query_policy,
)


def _stmt(doc, sid):
    return next(s for s in doc["Statement"] if s["Sid"] == sid)


def _maybe_stmt(doc, sid):
    return next((s for s in doc["Statement"] if s["Sid"] == sid), None)


def test_model_policy_has_one_statement_per_tier_gated_on_tag():
    doc = model_access_policy(region="us-east-1")
    sids = {s["Sid"] for s in doc["Statement"]}
    assert sids == {"InvokeTierOss", "InvokeTierMid", "InvokeTierFrontier"}
    for tier_cap, tier in [("Oss", "oss"), ("Mid", "mid"), ("Frontier", "frontier")]:
        s = _stmt(doc, f"InvokeTier{tier_cap}")
        assert s["Effect"] == "Allow"
        cond = s["Condition"]["StringEquals"]["aws:PrincipalTag/agate:tier"]
        assert cond == tier


def test_frontier_statement_includes_lower_tier_models():
    doc = model_access_policy(region="us-east-1")
    frontier = _stmt(doc, "InvokeTierFrontier")["Resource"]
    # cumulative: an oss model ARN appears in the frontier allow set
    oss_arn = foundation_model_arn(TIER_MODELS["oss"][0], region="us-east-1")
    frontier_arn = foundation_model_arn(TIER_MODELS["frontier"][0], region="us-east-1")
    assert oss_arn in frontier
    assert frontier_arn in frontier


def test_oss_statement_excludes_frontier_models():
    doc = model_access_policy(region="us-east-1")
    oss = _stmt(doc, "InvokeTierOss")["Resource"]
    frontier_arn = foundation_model_arn(TIER_MODELS["frontier"][0], region="us-east-1")
    assert frontier_arn not in oss


def test_data_scope_interpolates_tenant_tag_and_fails_closed():
    doc = data_scope_policy()
    # GetObject is gated by the resource ARN (tenant interpolated); ListBucket by prefix.
    get = _stmt(doc, "GetOwnTenantDocs")
    assert any("${aws:PrincipalTag/agate:tenant}" in r for r in get["Resource"])
    assert "Condition" not in get  # s3:prefix is NOT populated for GetObject
    lst = _stmt(doc, "ListOwnTenantDocs")
    assert "${aws:PrincipalTag/agate:tenant}" in lst["Condition"]["StringLike"]["s3:prefix"][0]
    deny = _stmt(doc, "DenyWhenNoTenantTag")
    assert deny["Effect"] == "Deny"
    assert deny["Condition"]["Null"]["aws:PrincipalTag/agate:tenant"] == "true"


def test_scope_confinement_denies_are_null_guarded_and_vectors_untouched():
    # #80: the scope-confinement Denies fire ONLY when agate:scope is present
    # (Null:false), so an unscoped session is unaffected (no regression).
    doc = data_scope_policy()
    get_deny = _stmt(doc, "DenyS3GetOutsideScopeSubtree")
    assert get_deny["Effect"] == "Deny"
    assert get_deny["Condition"]["Null"]["aws:PrincipalTag/agate:scope"] == "false"
    # confined to {tenant}/{scope}/ via NotResource (two interpolated principal tags)
    assert get_deny["NotResource"] == [
        "arn:aws:s3:::agate-docs-*/${aws:PrincipalTag/agate:tenant}/${aws:PrincipalTag/agate:scope}/*"
    ]
    list_deny = _stmt(doc, "DenyS3ListOutsideScopeSubtree")
    assert list_deny["Condition"]["Null"]["aws:PrincipalTag/agate:scope"] == "false"
    assert "StringNotLike" in list_deny["Condition"]


def test_browser_role_has_no_vector_query_grant():
    # #84: the vector query Allow moved OFF the browser-held role to agate-vector-reader,
    # so a direct QueryVectors with the browser's creds has no Allow (denied).
    doc = data_scope_policy()
    assert _maybe_stmt(doc, "QueryOwnTenantVectors") is None
    assert "s3vectors:QueryVectors" not in str(
        [s for s in doc["Statement"] if s["Effect"] == "Allow"]
    )
    # The fail-closed guard still lists s3vectors:* as defense-in-depth.
    assert "s3vectors:*" in _stmt(doc, "DenyWhenNoTenantTag")["Action"]


def test_vector_query_policy_is_tenant_fenced_and_guarded():
    # #84: the reader role's policy — the ONLY vector query grant — is tenant-fenced
    # by ResourceTag==PrincipalTag, with a no-tenant deny. Scope is NOT here (enforced
    # by the proxy injecting the filter; it isn't IAM-enforceable for vectors).
    doc = vector_query_policy()
    allow = _stmt(doc, "QueryOwnTenantVectors")
    assert allow["Effect"] == "Allow"
    assert allow["Action"] == ["s3vectors:QueryVectors", "s3vectors:GetVectors"]
    assert (
        allow["Condition"]["StringEquals"]["aws:ResourceTag/agate:tenant"]
        == "${aws:PrincipalTag/agate:tenant}"
    )
    assert "agate:scope" not in str(allow)
    deny = _stmt(doc, "DenyVectorsWhenNoTenantTag")
    assert deny["Effect"] == "Deny"
    assert deny["Condition"]["Null"]["aws:PrincipalTag/agate:tenant"] == "true"


# --- agent_write_policy (#118 deploy-on-confirm) ----------------------------


def test_agent_write_is_tenant_fenced_to_agents_segment():
    doc = agent_write_policy()
    allow = _stmt(doc, "PutOwnTenantAgents")
    assert allow["Action"] == ["s3:PutObject"]
    # confined to the tenant prefix AND the _agents/ segment (interpolated tenant tag), in
    # BOTH the scoped (`{tenant}/{scope}/_agents/*`) and unscoped tenant-root
    # (`{tenant}/_agents/*`) forms — the latter is the common tenant-wide author.
    assert allow["Resource"] == [
        "arn:aws:s3:::agate-docs-*/${aws:PrincipalTag/agate:tenant}/*/_agents/*",
        "arn:aws:s3:::agate-docs-*/${aws:PrincipalTag/agate:tenant}/_agents/*",
    ]
    # every Allowed ARN stays under the tenant tag + the _agents/ segment
    for r in allow["Resource"]:
        assert "${aws:PrincipalTag/agate:tenant}" in r
        assert "/_agents/" in r


def test_agent_write_fails_closed_without_tenant_tag():
    deny = _stmt(agent_write_policy(), "DenyAgentWriteWhenNoTenantTag")
    assert deny["Effect"] == "Deny"
    assert deny["Action"] == ["s3:PutObject"]
    assert deny["Condition"]["Null"]["aws:PrincipalTag/agate:tenant"] == "true"


def test_agent_write_scope_confinement_is_null_guarded():
    # A scoped session may only write under {tenant}/{scope}/_agents/* (Null:false guard ->
    # inert for an unscoped session).
    deny = _stmt(agent_write_policy(), "DenyAgentWriteOutsideScopeSubtree")
    assert deny["Effect"] == "Deny"
    assert deny["Condition"]["Null"]["aws:PrincipalTag/agate:scope"] == "false"
    assert deny["NotResource"] == [
        "arn:aws:s3:::agate-docs-*/${aws:PrincipalTag/agate:tenant}/${aws:PrincipalTag/agate:scope}/_agents/*"
    ]


def test_agent_write_honours_explicit_bucket():
    doc = agent_write_policy(bucket="agate-docs-123-us-east-1")
    allow = _stmt(doc, "PutOwnTenantAgents")
    assert "agate-docs-123-us-east-1" in allow["Resource"][0]
