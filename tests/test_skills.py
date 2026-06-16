"""Unit tests for Skills — governed capability packages (#119). No AWS.

The §8.6/§10 invariant: a Skill is a portable, reviewed bundle of capabilities — listing it
is sugar for listing its capabilities, so it compiles to the SAME scope/tier-clamped IAM and
can NEVER grant what the author couldn't declare directly.
"""

from __future__ import annotations

import pytest
from agate.agentcompile import compile_agent
from agate.agentspec import SpecError, parse_spec
from agate.skills import (
    Skill,
    SkillError,
    get_skill,
    register_skill,
    skill_capabilities,
    skill_catalog,
    validate_skill,
)


def _spec(**over):
    d = {"agent": "r", "description": "d", "role": "researcher", "scope": "lab"}
    d.update(over)
    return parse_spec(d)


# --- registry ----------------------------------------------------------------


def test_reference_skills_registered():
    names = {c["name"] for c in skill_catalog()}
    assert {"lit-reviewer", "hpc-analyst"} <= names


def test_get_unknown_skill_fails_closed():
    with pytest.raises(SkillError):
        get_skill("nonesuch")


def test_skill_capabilities_returns_the_bundle():
    assert set(skill_capabilities("lit-reviewer")) == {
        "library-search",
        "course-materials-reader",
    }


# --- the headline: a skill can't invent a grant -----------------------------


def test_skill_capabilities_are_all_real():
    # Every reference skill's capabilities must exist in the #113 catalog.
    for c in skill_catalog():
        validate_skill(get_skill(c["name"]))  # raises if any capability is uncatalogued


def test_skill_naming_an_uncatalogued_capability_fails_validation():
    bogus = Skill(
        name="bogus-skill", title="x", description="x",
        capabilities=("library-search", "delete-the-internet"),
    )
    with pytest.raises(SpecError):  # get_capability raises SpecError for the bad one
        validate_skill(bogus)


def test_skill_with_unknown_pattern_fails_validation():
    bogus = Skill(name="bad-pat", title="x", description="x", pattern="no-such-pattern")
    with pytest.raises(Exception):  # noqa: B017 — PatternError
        validate_skill(bogus)


def test_duplicate_skill_registration_rejected():
    with pytest.raises(SkillError):
        register_skill(Skill(name="lit-reviewer", title="dup", description="dup"))


# --- expansion into the spec's effective tools ------------------------------


def test_skills_expand_into_effective_tools():
    s = _spec(skills=["lit-reviewer"])
    assert s.skills == ("lit-reviewer",)  # declared skills preserved for audit
    assert set(s.tools) == {"library-search", "course-materials-reader"}


def test_skills_and_tools_union_is_deduped():
    # A capability listed both directly and via a skill appears once.
    s = _spec(reasoning="lit-review", tools=["library-search"], skills=["lit-reviewer"])
    assert sorted(s.tools) == ["course-materials-reader", "library-search"]


def test_unknown_skill_in_spec_fails_closed():
    with pytest.raises(SpecError):
        _spec(skills=["nonesuch"])


# --- the proof that matters: skills == equivalent explicit tools ------------


def test_skills_only_compiles_to_same_policy_as_explicit_tools():
    via_skill = compile_agent(_spec(skills=["lit-reviewer"]))
    via_tools = compile_agent(
        _spec(reasoning="lit-review", tools=["library-search", "course-materials-reader"])
    )
    assert via_skill.tool_policy == via_tools.tool_policy


# --- skill -> reasoning fill is conservative --------------------------------


def test_skill_pattern_fills_reasoning_when_absent():
    s = _spec(skills=["lit-reviewer"])  # no explicit reasoning
    assert s.reasoning.key == "lit-review"


def test_explicit_reasoning_always_wins_over_skill_pattern():
    s = _spec(reasoning="red-team", skills=["lit-reviewer"])
    assert s.reasoning.key == "red-team"


def test_skill_without_pattern_does_not_supply_reasoning():
    # hpc-analyst has no pattern; with no explicit reasoning, parse must still require one.
    with pytest.raises(SpecError):
        _spec(skills=["hpc-analyst"])
