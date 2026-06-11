"""Unit tests for the generated IAM policy documents. No AWS — pure JSON shape."""

from __future__ import annotations

from agg.entitlements import TIER_MODELS, foundation_model_arn
from policy.generate import data_scope_policy, model_access_policy


def _stmt(doc, sid):
    return next(s for s in doc["Statement"] if s["Sid"] == sid)


def test_model_policy_has_one_statement_per_tier_gated_on_tag():
    doc = model_access_policy(region="us-east-1")
    sids = {s["Sid"] for s in doc["Statement"]}
    assert sids == {"InvokeTierOss", "InvokeTierMid", "InvokeTierFrontier"}
    for tier_cap, tier in [("Oss", "oss"), ("Mid", "mid"), ("Frontier", "frontier")]:
        s = _stmt(doc, f"InvokeTier{tier_cap}")
        assert s["Effect"] == "Allow"
        cond = s["Condition"]["StringEquals"]["aws:PrincipalTag/agg:tier"]
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
    read = _stmt(doc, "ReadOwnTenantDocs")
    assert any("${aws:PrincipalTag/agg:tenant}" in r for r in read["Resource"])
    deny = _stmt(doc, "DenyWhenNoTenantTag")
    assert deny["Effect"] == "Deny"
    assert deny["Condition"]["Null"]["aws:PrincipalTag/agg:tenant"] == "true"
