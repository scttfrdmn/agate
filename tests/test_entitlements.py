"""Unit tests for the tier/entitlement single source of truth. No AWS."""

from __future__ import annotations

import pytest
from agg.entitlements import (
    TIER_MODELS,
    derive_tier,
    foundation_model_arn,
    model_arns_for_tier,
    models_for_tier,
)


def test_tiers_are_cumulative():
    oss = set(models_for_tier("oss"))
    mid = set(models_for_tier("mid"))
    frontier = set(models_for_tier("frontier"))
    assert oss < mid < frontier  # strict supersets
    assert oss == set(TIER_MODELS["oss"])
    assert frontier == oss | set(TIER_MODELS["mid"]) | set(TIER_MODELS["frontier"])


def test_oss_session_cannot_reach_frontier_models():
    oss_arns = set(model_arns_for_tier("oss"))
    frontier_only = set(TIER_MODELS["frontier"])
    for m in frontier_only:
        assert foundation_model_arn(m) not in oss_arns


@pytest.mark.parametrize(
    "aff,tier", [("student", "oss"), ("staff", "mid"), ("researcher", "frontier")]
)
def test_derive_tier(aff, tier):
    assert derive_tier(aff) == tier


def test_grant_overrides():
    assert derive_tier("student", grant=True) == "frontier"


def test_foundation_vs_inference_profile_arn():
    mid = "anthropic.claude-opus-4-1-20250805-v1:0"
    fm = foundation_model_arn(mid, region="us-east-1")
    assert fm == f"arn:aws:bedrock:us-east-1::foundation-model/{mid}"
    ip = foundation_model_arn(f"us.{mid}", region="us-east-1", account="123")
    assert ip == f"arn:aws:bedrock:us-east-1:123:inference-profile/us.{mid}"


def test_inference_profile_needs_profile_and_underlying_fm_arns():
    # Live finding (2026-06-12): invoking a cross-region inference profile requires
    # InvokeModel on BOTH the profile ARN AND the underlying foundation-model ARN.
    from agg.entitlements import model_resource_arns

    arns = model_resource_arns(
        "us.anthropic.claude-opus-4-1-20250805-v1:0", region="us-east-1", account="123"
    )
    assert (
        "arn:aws:bedrock:us-east-1:123:inference-profile/us.anthropic.claude-opus-4-1-20250805-v1:0"
        in arns
    )
    # underlying FM with the us. prefix stripped, region-wildcarded for cross-region routing
    assert "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-1-20250805-v1:0" in arns


def test_plain_foundation_model_needs_only_its_own_arn():
    from agg.entitlements import model_resource_arns

    arns = model_resource_arns("openai.gpt-oss-20b-1:0", region="us-east-1")
    assert arns == ["arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-oss-20b-1:0"]


def test_tier_arns_include_underlying_fms_for_profiles():
    # frontier (cumulative) must list both profile + underlying FM ARNs for Claude.
    arns = model_arns_for_tier("frontier", region="us-east-1", account="123")
    assert any("inference-profile/us.anthropic" in a for a in arns)
    assert any(
        a == "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-1-20250805-v1:0"
        for a in arns
    )
