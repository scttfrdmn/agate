"""Unit tests for natural-language agent drafting — the disposer core (#118). No AWS.

The §8.5/§10 thesis: the LLM proposes, the compiler disposes. An untrusted JSON draft becomes
an agent only by passing parse_spec (unsafe is unrepresentable) AND delegate (over-broad is
clamped to the author, disjoint is rejected). Authority originates ONLY from the author's
verified SessionTags — never the draft.
"""

from __future__ import annotations

from agate.drafting import dispose_draft, draft_system_prompt
from agate.tags import SessionTags


def _author(scope="chemistry", tier="frontier", tenant="uni", aff="researcher"):
    return SessionTags(
        affiliation=aff, tenant=tenant, courses=(), tier=tier, role="member", scope=scope
    )


def _draft(**over):
    d = {
        "agent": "paper-sweep", "description": "summarize new papers", "role": "researcher",
        "scope": "chemistry/chem-101", "reasoning": "lit-review", "tools": ["library-search"],
    }
    d.update(over)
    return d


# --- happy path --------------------------------------------------------------


def test_well_formed_draft_within_reach_is_ok():
    out = dispose_draft(_draft(), _author("chemistry"), subject="prof")
    assert out.ok is True
    assert out.boundary is not None
    # the rendered plan names the tool + scope
    text = " ".join(out.summary())
    assert "library-search" in text or "library" in text.lower()
    assert out.boundary.scope == "chemistry/chem-101"


def test_outcome_carries_clamped_instance_for_deferred_deploy():
    out = dispose_draft(_draft(), _author("chemistry"), subject="prof")
    assert out.instance is not None
    assert out.instance.child_tags.scope == "chemistry/chem-101"
    assert out.instance.invoker_subject == "prof"


# --- the headline: clamp (nested) -------------------------------------------


def test_nested_scope_kept():
    out = dispose_draft(_draft(scope="chemistry/chem-101"), _author("chemistry"), subject="p")
    assert out.boundary.scope == "chemistry/chem-101"


def test_broader_draft_scope_clamps_down_to_author():
    # author at chem-101, draft asks the broader 'chemistry' -> clamps DOWN to the author's.
    out = dispose_draft(_draft(scope="chemistry"), _author("chemistry/chem-101"), subject="p")
    assert out.ok is True
    assert out.boundary.scope == "chemistry/chem-101"


def test_tier_clamps_to_author_floor():
    # an oss author drafting a frontier-role agent -> boundary tier is oss (min).
    out = dispose_draft(_draft(role="researcher"), _author("chemistry", tier="oss"), subject="s")
    assert out.boundary.tier == "oss"


# --- the headline: reject (disjoint / cross-tenant) -------------------------


def test_disjoint_scope_rejected_no_boundary():
    # author at chemistry, draft asks physics (a sibling subtree) -> rejected, fail-closed.
    out = dispose_draft(_draft(scope="physics"), _author("chemistry"), subject="prof")
    assert out.ok is False
    assert out.boundary is None
    assert out.instance is None
    assert "outside your own" in out.reason or "reject" in out.reason.lower()


# --- fail-closed parse (unsafe is unrepresentable) --------------------------


def test_unknown_tool_rejected():
    out = dispose_draft(_draft(tools=["delete-the-internet"]), _author("chemistry"), subject="p")
    assert out.ok is False
    assert "unknown tool" in out.reason or "invalid draft" in out.reason


def test_unknown_top_level_key_rejected():
    d = _draft()
    d["sudo"] = True
    out = dispose_draft(d, _author("chemistry"), subject="p")
    assert out.ok is False


def test_traversal_scope_rejected():
    out = dispose_draft(_draft(scope="chemistry/../physics"), _author("chemistry"), subject="p")
    assert out.ok is False


def test_non_dict_draft_rejected():
    out = dispose_draft("not a dict", _author("chemistry"), subject="p")  # type: ignore[arg-type]
    assert out.ok is False


def test_skills_draft_expands_and_is_disposed():
    # a draft using a SKILL (no explicit reasoning) is disposed like any other.
    d = {
        "agent": "r", "description": "d", "role": "researcher", "scope": "chemistry",
        "skills": ["lit-reviewer"],
    }
    out = dispose_draft(d, _author("chemistry"), subject="p")
    assert out.ok is True


# --- nothing compiles / assumes a role without confirmation -----------------


def test_dispose_is_pure_no_role_assumed():
    # dispose_draft must not touch STS/persistence — it only parses, clamps, renders.
    # (If it imported/called boto3 the import would surface; the outcome is plain data.)
    out = dispose_draft(_draft(), _author("chemistry"), subject="prof")
    assert isinstance(out.summary(), list)
    # the instance is a credential TEMPLATE (child_tags), not a vended credential
    assert not hasattr(out.instance, "credentials")


# --- the catalog-driven prompt ----------------------------------------------


def test_prompt_lists_real_catalog_names_and_author_ceiling():
    from agate.agentspec import capability_catalog
    from agate.skills import skill_catalog

    p = draft_system_prompt(_author("chemistry/chem-101", tier="mid"))
    for cap in capability_catalog():
        assert cap["name"] in p
    for skl in skill_catalog():
        assert skl["name"] in p
    assert "mid" in p  # author tier
    assert "chemistry/chem-101" in p  # author scope ceiling
