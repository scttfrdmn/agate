"""Unit tests for the generated IAM policy documents. No AWS — pure JSON shape."""

from __future__ import annotations

from agate.entitlements import TIER_MODELS, foundation_model_arn
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
    # vectors statement is NOT scope-confined (deferred): only the tenant ResourceTag.
    vec = _stmt(doc, "QueryOwnTenantVectors")
    assert vec["Condition"]["StringEquals"]["aws:ResourceTag/agate:tenant"]
    assert "agate:scope" not in str(vec)
