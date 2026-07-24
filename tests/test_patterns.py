"""Unit tests for composable reasoning patterns. No AWS."""

from __future__ import annotations

import pytest
from agate.patterns import (
    Pattern,
    PatternError,
    Role,
    catalog,
    compile_pattern,
    get,
)

ENTITLED = ["cheap-model", "mid-model", "best-model"]  # cheapest-first, as entitlements returns


def test_catalog_lists_registered_patterns():
    keys = {p["key"] for p in catalog()}
    assert {"lit-review", "red-team"} <= keys
    # every entry carries what the SPA picker needs
    for p in catalog():
        assert set(p) >= {"key", "title", "description", "mode"}


def test_get_unknown_pattern_raises():
    with pytest.raises(PatternError):
        get("does-not-exist")


def test_compile_debate_pattern_resolves_roles_to_entitled_models():
    pat = get("lit-review")
    payload = compile_pattern(pat, question="What does the evidence say?", entitled_models=ENTITLED)
    assert payload["mode"] == "DEBATE"
    roster = payload["roster"]
    assert [m["label"] for m in roster] == ["claims", "methods", "gaps"]
    # every chosen model is an entitled id (so dispatch's allow-check can't reject it)
    assert all(m["tier"] in ENTITLED for m in roster)
    # per-role system prompts are carried through (the institution's recipe)
    assert all(m["system"] for m in roster)
    # "best" preference -> highest entitled tier
    gaps = next(m for m in roster if m["label"] == "gaps")
    assert gaps["tier"] == "best-model"
    assert payload["adjudicator"]["tier"] == "best-model"


def test_model_preference_resolution():
    p = Pattern(
        key="x",
        title="x",
        description="x",
        mode="DEBATE",
        roles=(
            Role(label="a", system="s", model="cheapest"),
            Role(label="b", system="s", model="balanced"),
            Role(label="c", system="s", model="best"),
        ),
    )
    roster = compile_pattern(p, question="q", entitled_models=ENTITLED)["roster"]
    by = {m["label"]: m["tier"] for m in roster}
    assert by == {"a": "cheap-model", "b": "mid-model", "c": "best-model"}


def test_compile_passes_evidence_through():
    payload = compile_pattern(get("red-team"), question="q", entitled_models=ENTITLED, evidence="E")
    assert payload["evidence"] == "E"


def test_compile_with_no_entitled_models_fails_closed():
    with pytest.raises(PatternError):
        compile_pattern(get("lit-review"), question="q", entitled_models=[])


def test_synthesis_pattern_builds_generator_not_roster():
    p = Pattern(
        key="s",
        title="s",
        description="s",
        mode="SYNTHESIS",
        roles=(Role(label="ask", system="", model="cheapest"),),
    )
    payload = compile_pattern(p, question="q", entitled_models=ENTITLED)
    assert payload["generator"]["tier"] == "cheap-model"
    assert "roster" not in payload


def test_debate_pattern_without_roles_raises():
    p = Pattern(key="bad", title="b", description="b", mode="DEBATE", roles=())
    with pytest.raises(PatternError):
        compile_pattern(p, question="q", entitled_models=ENTITLED)
