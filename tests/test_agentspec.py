"""Unit tests for the agent spec schema + parser (#104). No AWS."""

from __future__ import annotations

import pytest
from agate.agentspec import (
    AgentSpec,
    BudgetSpec,
    SpecError,
    capability_catalog,
    get_capability,
    load_spec,
    parse_spec,
    role_to_tier,
)


def _base(**over) -> dict:
    d = {
        "agent": "chem101-ta",
        "description": "Drafts feedback for instructor review.",
        "role": "ta",
        "scope": "chemistry/chem-101",
        "reasoning": "lit-review",
    }
    d.update(over)
    return d


def test_valid_spec_round_trips():
    spec = parse_spec(_base(tools=["course-materials-reader"], memory="per-invoker"))
    assert isinstance(spec, AgentSpec)
    assert spec.name == "chem101-ta"
    assert spec.scope == "chemistry/chem-101"
    assert spec.tools == ("course-materials-reader",)
    assert spec.reasoning.mode == "DEBATE"  # resolved from the registry


# --- role -> tier (least privilege) -----------------------------------------


@pytest.mark.parametrize(
    "role,tier",
    [("student", "oss"), ("ta", "oss"), ("instructor", "mid"), ("researcher", "frontier")],
)
def test_role_to_tier(role, tier):
    assert role_to_tier(role) == tier


def test_unknown_role_falls_to_oss_floor():
    assert role_to_tier("wizard") == "oss"  # never raise-then-widen


def test_spec_cannot_self_escalate_tier():
    # SECURITY (#105 review): a spec must not be able to claim authority. There is no
    # `grant`/tier field — `grant: true` is an unknown key and is rejected, and tier is
    # bound by the declared role. Promotion is a property of the verified spawner (#106).
    with pytest.raises(SpecError, match="unknown spec keys"):
        parse_spec(_base(role="student", grant=True))
    # A student-role spec is oss regardless of any other field.
    assert parse_spec(_base(role="student")).tier == "oss"


# --- fail-closed validation -------------------------------------------------


def test_unknown_top_level_key_rejected():
    with pytest.raises(SpecError, match="unknown spec keys"):
        parse_spec(_base(superpower="god-mode"))


def test_missing_required_fields_rejected():
    for missing in ("description", "role"):
        d = _base()
        del d[missing]
        with pytest.raises(SpecError):
            parse_spec(d)


def test_garbled_scope_rejected_not_silently_tenant_wide():
    # A GIVEN scope that normalises to empty must fail, not become tenant-wide.
    with pytest.raises(SpecError, match="valid path"):
        parse_spec(_base(scope="///"))


def test_scope_path_traversal_rejected():
    with pytest.raises(SpecError):
        parse_spec(_base(scope="chemistry/../physics"))


def test_omitted_scope_is_tenant_wide():
    d = _base()
    del d["scope"]
    assert parse_spec(d).scope == ""


def test_unknown_tool_rejected():
    with pytest.raises(SpecError, match="unknown tool"):
        parse_spec(_base(tools=["delete-everything"]))


def test_unknown_reasoning_key_rejected():
    with pytest.raises(SpecError):
        parse_spec(_base(reasoning="no-such-pattern"))


def test_inline_reasoning_resolves():
    spec = parse_spec(
        _base(
            reasoning={
                "mode": "DEBATE",
                "roles": [{"label": "a", "system": "argue", "model": "best"}],
            }
        )
    )
    assert spec.reasoning.mode == "DEBATE"
    assert spec.reasoning.roles[0].label == "a"


def test_inline_debate_without_roles_rejected():
    with pytest.raises(SpecError, match="DEBATE"):
        parse_spec(_base(reasoning={"mode": "DEBATE", "roles": []}))


def test_bad_memory_and_visibility_rejected():
    with pytest.raises(SpecError):
        parse_spec(_base(memory="everything"))
    with pytest.raises(SpecError):
        parse_spec(_base(visibility="public-internet"))


# --- budget parsing ---------------------------------------------------------


def test_budget_string_parsed():
    spec = parse_spec(_base(budget="$20 / student / term"))
    assert spec.budget == BudgetSpec(usd=20.0, per="student", period_kind="term")


def test_budget_dict_parsed():
    spec = parse_spec(_base(budget={"usd": 50, "per": "scope", "period": "month"}))
    assert spec.budget == BudgetSpec(usd=50.0, per="scope", period_kind="month")


@pytest.mark.parametrize("bad", ["$-5 / student / term", "20 bucks weekly", "$20 / student"])
def test_malformed_budget_rejected(bad):
    with pytest.raises(SpecError):
        parse_spec(_base(budget=bad))


def test_nan_budget_rejected():
    with pytest.raises(SpecError, match="finite"):
        parse_spec(_base(budget={"usd": float("nan"), "per": "student", "period": "term"}))


# --- skills (#119): expand into effective tools, fail closed ----------------


def test_skills_expand_into_tools():
    spec = parse_spec(_base(skills=["lit-reviewer"]))
    assert spec.skills == ("lit-reviewer",)
    assert set(spec.tools) == {"library-search", "course-materials-reader"}


def test_unknown_skill_rejected():
    with pytest.raises(SpecError):
        parse_spec(_base(skills=["nonesuch"]))


def test_skills_must_be_a_list():
    with pytest.raises(SpecError, match="skills must be a list"):
        parse_spec(_base(skills="lit-reviewer"))


# --- invokers + triggers (shape only) ---------------------------------------


def test_invokers_parsed():
    spec = parse_spec(_base(invokers="roster:chem-101"))
    assert spec.invokers.kind == "roster" and spec.invokers.ref == "chem-101"


def test_bad_invoker_kind_rejected():
    with pytest.raises(SpecError):
        parse_spec(_base(invokers="magic:everything"))


def test_trigger_missing_fields_rejected():
    with pytest.raises(SpecError, match="on.*then|trigger"):
        parse_spec(_base(triggers=[{"on": "event:lms.submitted"}]))


def test_triggers_parsed_and_classified():
    spec = parse_spec(_base(triggers=[{"on": "event:lms.submitted", "then": "draft"}]))
    t = spec.triggers[0]
    assert t.on == "event:lms.submitted" and t.then == "draft"
    assert t.kind == "event" and t.detail == "lms.submitted"


def test_schedule_trigger_classified():
    spec = parse_spec(
        _base(triggers=[{"on": "schedule:cron(0 9 ? * MON *)", "then": "summarize"}])
    )
    t = spec.triggers[0]
    assert t.kind == "schedule" and t.detail == "cron(0 9 ? * MON *)"


def test_bad_trigger_kind_rejected():
    # A typo'd kind must fail closed, not silently no-op on an autonomous agent.
    with pytest.raises(SpecError, match="kind"):
        parse_spec(_base(triggers=[{"on": "lms:submitted", "then": "draft"}]))


def test_schedule_must_be_cron_or_rate():
    with pytest.raises(SpecError, match="cron.*rate|rate.*cron"):
        parse_spec(_base(triggers=[{"on": "schedule:every-monday", "then": "draft"}]))


def test_schedule_with_trailing_junk_rejected():
    # A complete expression is required — `cron(...)<trailing>` must fail at parse, not slip
    # through to the deploy phase.
    with pytest.raises(SpecError, match="cron.*rate|rate.*cron"):
        parse_spec(_base(triggers=[{"on": "schedule:cron(0 9 ? * MON *) drop", "then": "x"}]))


# --- capability catalog -----------------------------------------------------


def test_capability_catalog_has_reference_caps():
    names = {c["name"] for c in capability_catalog()}
    assert {"course-materials-reader", "gradebook-drafts"} <= names


def test_gradebook_drafts_is_a_write_capability():
    assert get_capability("gradebook-drafts").grant.write is True
    assert get_capability("course-materials-reader").grant.write is False


# --- campus MCP tools (#113/#114) -------------------------------------------


def test_catalog_includes_campus_tools():
    names = {c["name"] for c in capability_catalog()}
    assert {"library-search", "lms-read", "sis-self-read", "hpc-submit", "hpc-monitor"} <= names


def test_campus_tools_are_gateway_tools():
    for name in ("library-search", "lms-read", "sis-self-read", "hpc-submit", "hpc-monitor"):
        assert get_capability(name).grant.resource_kind == "gateway-tool"


def test_hpc_submit_is_a_write_monitor_is_read():
    assert get_capability("hpc-submit").grant.write is True  # the flagship "agent that acts"
    assert get_capability("hpc-monitor").grant.write is False


def test_spec_can_declare_hpc_tools():
    spec = parse_spec(_base(role="researcher", tools=["hpc-submit", "hpc-monitor"]))
    assert spec.tools == ("hpc-submit", "hpc-monitor")


# --- YAML edge --------------------------------------------------------------


def test_load_spec_parses_yaml_or_skips_without_pyyaml():
    yaml = pytest.importorskip("yaml")  # skip cleanly if pyyaml absent (it's optional)
    doc = yaml.dump(_base(tools=["course-materials-reader"]))
    spec = load_spec(doc)
    assert spec.name == "chem101-ta"


def test_load_spec_rejects_non_mapping():
    pytest.importorskip("yaml")
    with pytest.raises(SpecError):
        load_spec("- just\n- a\n- list\n")
