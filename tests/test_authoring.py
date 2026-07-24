"""Unit tests for graphical agent authoring — the bounded-menu core (#117). No AWS.

The §8.5/§10 invariant: graphical authoring is the safest surface — the menu offers ONLY what
the author holds (unsafe is unrepresentable), AND every selection funnels through the same
disposer (#118) that clamps to the author (so even a bypassed UI can't widen reach).
"""

from __future__ import annotations

from agate.agentspec import capability_catalog
from agate.authoring import (
    author_from_options,
    authoring_options,
    build_spec,
    get_template,
    offerable_scopes,
    offerable_tiers,
    template_gallery,
)
from agate.drafting import dispose_draft
from agate.skills import skill_catalog
from agate.tags import SessionTags


def _author(scope="chemistry", tier="mid", tenant="uni", aff="faculty"):
    return SessionTags(
        affiliation=aff, tenant=tenant, courses=(), tier=tier, role="member", scope=scope
    )


_NODES = ("chemistry", "chemistry/chem-101", "physics", "")


# --- the headline: the scope menu is bounded --------------------------------


def test_scope_menu_offers_only_held_nodes():
    offered = set(offerable_scopes("chemistry", _NODES))
    assert offered == {"chemistry", "chemistry/chem-101"}  # NOT physics, NOT the tenant root


def test_disjoint_author_never_offered_anothers_scope():
    assert "chemistry" not in offerable_scopes("physics", _NODES)
    assert "chemistry/chem-101" not in offerable_scopes("physics", _NODES)


def test_author_own_scope_always_offerable_even_with_no_candidates():
    assert offerable_scopes("chemistry/chem-101", ()) == ("chemistry/chem-101",)


def test_tenant_wide_author_offered_everything_in_candidates():
    # an unscoped (tenant-wide) author contains every node
    offered = set(offerable_scopes("", _NODES))
    assert "chemistry" in offered and "physics" in offered


# --- the tier menu is bounded -----------------------------------------------


def test_tier_menu_at_or_below_author():
    assert offerable_tiers("oss") == ("oss",)
    assert offerable_tiers("mid") == ("oss", "mid")
    assert offerable_tiers("frontier") == ("oss", "mid", "frontier")


# --- the catalogs + grammar menus surfaced ----------------------------------


def test_options_surface_catalogs_and_grammar():
    opts = authoring_options(_author(), _NODES)
    cap_names = {c["name"] for c in opts.capabilities}
    assert {c["name"] for c in capability_catalog()} == cap_names
    assert {s["name"] for s in skill_catalog()} <= {s["name"] for s in opts.skills}
    assert "scope" in opts.budget_per and "month" in opts.budget_periods
    assert "schedule" in opts.trigger_kinds and "event" in opts.trigger_kinds
    # the menu is pre-clamped to the author
    assert opts.author_tier == "mid"
    assert set(opts.offerable_tiers) == {"oss", "mid"}
    assert set(opts.offerable_scopes) == {"chemistry", "chemistry/chem-101"}


def test_options_to_dict_is_json_shaped():
    d = authoring_options(_author(), _NODES).to_dict()
    assert isinstance(d["offerable_scopes"], list)
    assert isinstance(d["capabilities"], list)


# --- template gallery -> spec -> disposed -----------------------------------


def test_template_gallery_lists_templates():
    ids = {t["id"] for t in template_gallery()}
    assert {"paper-monitor", "gradebook-drafter", "lab-librarian"} <= ids


def test_get_template_returns_a_copy():
    t1 = get_template("paper-monitor")
    t1["agent"] = "mutated"
    t2 = get_template("paper-monitor")
    assert t2["agent"] != "mutated"  # the shared template wasn't mutated


def test_unknown_template_is_none():
    assert get_template("nonesuch") is None


def test_filled_template_disposes_to_clamped_boundary():
    t = get_template("paper-monitor")
    t["scope"] = "chemistry/chem-101"
    out = author_from_options(t, _author(scope="chemistry"), subject="prof")
    assert out.ok is True
    assert out.boundary.scope == "chemistry/chem-101"


# --- the structural proof: no offered option produces an over-broad spec ----


def test_broadest_offered_selection_stays_within_author():
    author = _author(scope="chemistry", tier="mid")
    opts = authoring_options(author, _NODES)
    # pick the BROADEST offered scope + the HIGHEST offered tier + every catalog tool
    broadest_scope = "chemistry"  # the author's own / broadest offered
    assert broadest_scope in opts.offerable_scopes
    spec = build_spec(
        agent="max",
        description="d",
        role="researcher",  # researcher -> frontier ask
        scope=broadest_scope,
        reasoning="lit-review",
        tools=tuple(c["name"] for c in opts.capabilities),
    )
    out = author_from_options(spec, author, subject="prof")
    assert out.ok is True
    # clamped: tier never exceeds the author's mid; scope stays within chemistry
    from agate.delegate import _contains
    from agate.entitlements import TIER_RANK

    assert TIER_RANK[out.boundary.tier] <= TIER_RANK["mid"]
    assert _contains("chemistry", out.boundary.scope)


def test_forged_selection_outside_menu_is_rejected_by_disposer():
    # belt-and-suspenders: a scope NOT in the offered set (a bypassed UI) is still clamped/
    # rejected by the disposer — the menu is a convenience, the compiler is the authority.
    author = _author(scope="chemistry")
    forged = build_spec(
        agent="evil", description="d", role="researcher", scope="physics", reasoning="lit-review"
    )
    out = author_from_options(forged, author, subject="prof")
    assert out.ok is False


# --- funnel parity: the builder is just a spec-dict front-end ---------------


def test_author_from_options_equals_dispose_draft():
    author = _author(scope="chemistry")
    spec = build_spec(
        agent="r",
        description="d",
        role="researcher",
        scope="chemistry/chem-101",
        reasoning="lit-review",
        tools=("library-search",),
    )
    via_builder = author_from_options(spec, author, subject="prof")
    via_draft = dispose_draft(spec, author, subject="prof")
    assert via_builder.ok == via_draft.ok
    assert via_builder.boundary.to_dict() == via_draft.boundary.to_dict()
