"""Graphical agent authoring — the bounded-menu core (#117, vision §8.5).

The beginner-first authoring ladder — **template gallery** (fill 2 blanks) → **visual
builder** (tree-scope picker + capability checklist + when-X-do-Y rules) → **graph editor** —
all rungs round-tripping to ONE spec dict. The load-bearing insight: graphical authoring is
the *safest* surface, not a dumbed-down one. Two independent guarantees, either of which
suffices:

  1. **Unrepresentable (the UX layer):** `authoring_options` offers ONLY tiers ≤ the author's
     and scope nodes the author CONTAINS — so the builder literally cannot render an
     over-broad button (escalation = the absence of the button).
  2. **Disposed (the boundary, belt-and-suspenders):** every selection funnels through the
     #118 `drafting.dispose_draft` (parse → clamp to the author → render), so even a client
     that bypasses the UI and POSTs a hand-crafted selection is clamped or rejected exactly as
     an LLM draft is. The menu is a UX convenience; the COMPILER is the authority.

So the template gallery, the visual builder, the graph editor, and natural-language drafting
(#118) are four front-ends to the ONE disposer — a beginner is exactly as bounded as a YAML
author. This module is PURE and AWS-free: it enumerates a bounded menu and assembles a spec
dict. The SPA UI + the `/authoring` endpoint + the tenant scope-tree data source (which
supplies `candidate_scope_nodes`) are deferred consumers.
"""

from __future__ import annotations

from dataclasses import dataclass

from agate.agentspec import (
    _BUDGET_PER,
    _PERIOD_KINDS,
    _TRIGGER_KINDS,
    capability_catalog,
)
from agate.delegate import _contains
from agate.drafting import DraftOutcome, dispose_draft
from agate.entitlements import TIER_RANK, TIERS, Tier
from agate.patterns import catalog as pattern_catalog
from agate.skills import skill_catalog
from agate.tags import SessionTags


@dataclass(frozen=True, slots=True)
class AuthoringOptions:
    """The bounded menu the visual builder renders. Every selectable value is pre-clamped to
    the author's reach — the UI cannot present a scope the author doesn't hold or a tier above
    theirs. Pure data, JSON-serialisable for the deferred authoring endpoint."""

    author_tier: Tier
    author_scope: str
    offerable_tiers: tuple[Tier, ...]
    offerable_scopes: tuple[str, ...]
    capabilities: tuple[dict, ...]
    skills: tuple[dict, ...]
    patterns: tuple[dict, ...]
    budget_per: tuple[str, ...]
    budget_periods: tuple[str, ...]
    trigger_kinds: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "author_tier": self.author_tier,
            "author_scope": self.author_scope,
            "offerable_tiers": list(self.offerable_tiers),
            "offerable_scopes": list(self.offerable_scopes),
            "capabilities": [dict(c) for c in self.capabilities],
            "skills": [dict(s) for s in self.skills],
            "patterns": [dict(p) for p in self.patterns],
            "budget_per": list(self.budget_per),
            "budget_periods": list(self.budget_periods),
            "trigger_kinds": list(self.trigger_kinds),
        }


def offerable_scopes(author_scope: str, candidate_scope_nodes: tuple[str, ...]) -> tuple[str, ...]:
    """The scope nodes the picker may offer: those the author CONTAINS (`delegate._contains`,
    the exact containment the clamp uses), plus the author's OWN scope (always offerable, even
    when the candidate list is empty). A `chemistry` author is offered `chemistry`,
    `chemistry/chem-101`, … but never `physics` or the bare tenant root (unless tenant-wide).
    Order-stable, deduped. The candidate list is INJECTED (tenant data) — this only filters."""
    out: list[str] = []
    seen: set[str] = set()
    # The author's own scope is always a legal choice (authoring within one's own reach).
    own = author_scope.strip("/")
    for node in (own, *(str(n).strip("/") for n in candidate_scope_nodes)):
        if node in seen:
            continue
        if _contains(author_scope, node):  # author contains it (own scope contains itself)
            seen.add(node)
            out.append(node)
    return tuple(out)


def offerable_tiers(author_tier: Tier) -> tuple[Tier, ...]:
    """Tiers at or below the author's (`TIER_RANK`) — the picker never offers a tier the
    author can't grant. An oss author is offered (oss,); a mid author (oss, mid)."""
    rank = TIER_RANK[author_tier]
    return tuple(t for t in TIERS if TIER_RANK[t] <= rank)


def authoring_options(
    author_tags: SessionTags, candidate_scope_nodes: tuple[str, ...] = ()
) -> AuthoringOptions:
    """The bounded menu for an author. Every selectable scope/tier is pre-clamped to the
    author's reach (unsafe is unrepresentable); the catalogs + grammar menus are the
    checklists/fields the builder renders. Pure — no AWS, no model."""
    return AuthoringOptions(
        author_tier=author_tags.tier,
        author_scope=author_tags.scope,
        offerable_tiers=offerable_tiers(author_tags.tier),
        offerable_scopes=offerable_scopes(author_tags.scope, tuple(candidate_scope_nodes)),
        capabilities=tuple(capability_catalog()),
        skills=tuple(skill_catalog()),
        patterns=tuple(pattern_catalog()),
        budget_per=tuple(sorted(_BUDGET_PER)),
        budget_periods=tuple(sorted(_PERIOD_KINDS)),
        trigger_kinds=tuple(sorted(_TRIGGER_KINDS)),
    )


# --- template gallery (the lowest rung) -------------------------------------
# A template is a spec-dict SKELETON with a couple of author-filled slots (scope, name). Each
# composes EXISTING capabilities/skills/patterns — no new compiler logic. Filling a template
# and funneling it through `author_from_options`/`dispose_draft` clamps it to the author like
# any other spec.

_TEMPLATES: dict[str, dict] = {
    "paper-monitor": {
        "agent": "paper-monitor",
        "description": "Summarize new papers in your scope on a weekly schedule.",
        "role": "researcher",
        "skills": ["lit-reviewer"],
        "budget": "$20 / user / month",
        "triggers": [{"on": "schedule:rate(7 days)", "then": "summarize"}],
        "memory": "personal",
    },
    "gradebook-drafter": {
        "agent": "gradebook-drafter",
        "description": "Draft feedback on submissions for instructor review (never live).",
        "role": "ta",
        "reasoning": "lit-review",
        "tools": ["course-materials-reader", "gradebook-drafts"],
        "budget": "$10 / student / term",
        "memory": "none",
    },
    "lab-librarian": {
        "agent": "lab-librarian",
        "description": "Answer questions from your lab's documents and the library catalog.",
        "role": "researcher",
        "skills": ["lit-reviewer"],
        "budget": "$15 / user / month",
        "memory": "shared",
    },
}


def template_gallery() -> list[dict]:
    """The gallery picker rows: id + name + description (no spec internals)."""
    return [
        {"id": tid, "name": t.get("agent", tid), "description": t.get("description", "")}
        for tid, t in _TEMPLATES.items()
    ]


def get_template(template_id: str) -> dict | None:
    """The spec-dict skeleton for a template id, or None. Returns a COPY so a caller filling
    its blanks (scope, name) can't mutate the shared template."""
    t = _TEMPLATES.get(template_id)
    return dict(t) if t is not None else None


# --- the build helper + the funnel ------------------------------------------


def build_spec(
    *,
    agent: str,
    description: str,
    role: str,
    scope: str = "",
    reasoning: str | None = None,
    tools: tuple[str, ...] = (),
    skills: tuple[str, ...] = (),
    budget: str | None = None,
    triggers: tuple[dict, ...] = (),
) -> dict:
    """Assemble a spec dict from the builder's structured selections (or a filled template).
    Pure dict assembly — empty optional fields are OMITTED so `parse_spec`'s defaults apply
    (and so a skill can supply the reasoning when none is picked, #119). This is what the
    visual builder's form state maps to before it funnels through the disposer."""
    spec: dict = {"agent": agent, "description": description, "role": role}
    if scope:
        spec["scope"] = scope
    if reasoning:
        spec["reasoning"] = reasoning
    if tools:
        spec["tools"] = list(tools)
    if skills:
        spec["skills"] = list(skills)
    if budget:
        spec["budget"] = budget
    if triggers:
        spec["triggers"] = [dict(t) for t in triggers]
    return spec


def author_from_options(
    spec: dict, author_tags: SessionTags, *, subject: str
) -> DraftOutcome:
    """Funnel a builder-assembled (or template-filled) spec dict through the SAME disposer an
    LLM draft uses (#118): parse → clamp to the author → render the effective boundary to
    confirm. So the boundary is enforced by the compiler, never the UI — a forged selection
    naming a scope outside the author's is clamped or rejected, identical to a hallucinated
    LLM draft. Returns the `DraftOutcome` (bounded plan to confirm, or a fail-closed reason).
    Performs NO assume-role / persistence — deploy-on-confirm is deferred."""
    return dispose_draft(spec, author_tags, subject=subject)
