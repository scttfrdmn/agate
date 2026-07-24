"""Natural-language agent drafting — the disposer core (#118, vision §8.5).

The ultimate beginner authoring surface: a user types *"an agent that summarizes new papers
in my lab every Monday"* → an LLM **drafts** a spec → the compiler **clamps** it to what the
author actually holds → renders the bounded plan for human confirmation; nothing compiles
without it. The load-bearing thesis — **the LLM proposes, the compiler disposes**: authority
NEVER originates from the model's suggestion, only from the author's verified entitlement.

This module is PURE and AWS-free. The LLM's output is a *string of JSON* with ZERO authority.
It becomes an agent only by passing two existing gates, fail-closed at each step:
  1. `agentspec.parse_spec` (#104) — unsafe is unrepresentable: unknown keys/tools/skills, a
     `..` scope, a bad budget all raise. A malformed draft never yields a partial spec.
  2. `delegate.delegate` (#106) — the clamp: scope ∩ author, tier = min(author, spec), role
     forced member. A draft scope NESTED under the author's narrows DOWN; a DISJOINT or
     cross-tenant scope is REJECTED (never silently widened). Authority comes ONLY from the
     author's `SessionTags` — never the draft.
Then `boundary.describe_instantiated` (#108) renders the CLAMPED credential as the legible
"reads X · may draft Y · ≤ $Z · runs Mondays" plan the human confirms. `dispose_draft` only
parses, clamps, and renders — it NEVER assumes a role or persists; the deploy-on-confirm step
is the deferred executor (like #115/#136). So the beginner surface is exactly as bounded as
the expert one: "unsafe is unrepresentable" extends to natural language for free.

The live entitled-model draft call (the user's own tier) and the confirm UI consume this
pure disposer; both are deferred. `draft_system_prompt` builds the catalog-driven prompt that
tells the model which real tools/skills/patterns exist + the author's ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass

from agate.agentspec import SpecError, capability_catalog, parse_spec
from agate.boundary import EffectiveBoundary, describe_instantiated
from agate.delegate import (
    DelegationError,
    InstantiatedAgent,
    delegate,
    invoker_namespace,
)
from agate.patterns import catalog as pattern_catalog
from agate.skills import skill_catalog
from agate.tags import SessionTags


def draft_system_prompt(author_tags: SessionTags) -> str:
    """Build the drafter's system prompt: the MENU of real capabilities/skills/patterns the
    model may use + the author's OWN ceiling (tier, scope) it must draft within + the spec
    shape. The model is told to emit ONLY a JSON spec dict using these names. This is a
    QUALITY aid, not the boundary — a hallucination past it is caught by `dispose_draft`
    (parse + clamp); the boundary holds regardless of what the model emits."""
    caps = ", ".join(c["name"] for c in capability_catalog())
    skls = ", ".join(s["name"] for s in skill_catalog())
    pats = ", ".join(p["key"] for p in pattern_catalog())
    scope = author_tags.scope or "(tenant-wide)"
    return (
        "You draft an agate agent spec from a user's request. Output ONLY a JSON object with "
        "these fields: agent (name), description, role, scope, reasoning (a pattern key), "
        "tools (list), skills (list), budget (e.g. '$20 / user / month'), triggers "
        "(list of {on, then}). Use ONLY these names:\n"
        f"  tools: {caps}\n"
        f"  skills: {skls}\n"
        f"  reasoning patterns: {pats}\n"
        "You MUST stay within the author's authority — you cannot grant more than they hold:\n"
        f"  author tier: {author_tags.tier}\n"
        f"  author scope: {scope}\n"
        "Draft a scope at or below the author's. Anything broader or outside it will be "
        "clamped or rejected. Emit valid JSON only — no prose, no code fences."
    )


@dataclass(frozen=True, slots=True)
class DraftOutcome:
    """The result of disposing an LLM draft. `ok=True` carries the bounded plan to confirm;
    `ok=False` carries the fail-closed reason (parse failure, or a scope outside the author's
    authority). `instance` is the CLAMPED `InstantiatedAgent` the deferred deploy-on-confirm
    step uses (its `child_tags` are the author-narrowed credential). Nothing has compiled to
    a live agent — this is pure data for the human to confirm."""

    ok: bool
    reason: str = ""
    boundary: EffectiveBoundary | None = None
    instance: InstantiatedAgent | None = None

    def summary(self) -> list[str]:
        """The legible plan lines for the confirm step (the reason line when rejected)."""
        if self.boundary is None:
            return [f"rejected: {self.reason}"]
        return self.boundary.summary()


def dispose_draft(draft: dict, author_tags: SessionTags, *, subject: str) -> DraftOutcome:
    """Dispose an UNTRUSTED draft dict (an LLM's JSON output) against the author's VERIFIED
    `SessionTags`. Runs the existing pure pipeline, fail-closed at each step:

      parse_spec (unsafe is unrepresentable) → delegate (clamp to the author, reject disjoint)
      → describe_instantiated (render the clamped boundary for confirmation).

    Returns a `DraftOutcome` — never raises for an expected failure (a bad draft / an
    over-reaching scope is a `ok=False` outcome with a reason, surfaced to the user). Authority
    originates ONLY from `author_tags`; the draft is a suggestion the disposer bounds or
    refuses. Performs NO assume-role and NO persistence — the deploy-on-confirm step is
    deferred (#106 `spawn_child`)."""
    if not isinstance(draft, dict):
        return DraftOutcome(ok=False, reason="draft must be a JSON object")

    # Step 1 — parse (the disposer): unsafe is unrepresentable.
    try:
        spec = parse_spec(draft)
    except SpecError as exc:
        return DraftOutcome(ok=False, reason=f"invalid draft: {exc}")

    # Step 2 — clamp (the headline): narrow to the author's verified reach. A draft scope
    # nested under the author's clamps down; a disjoint/cross-tenant scope is rejected. The
    # author is the SPAWNER here (self-authoring), so we delegate directly — not the
    # third-party invoker-eligibility path.
    try:
        child_tags = delegate(author_tags, spec, subject=subject)
    except DelegationError as exc:
        return DraftOutcome(
            ok=False,
            reason=f"draft requests authority outside your own — clamped/rejected: {exc}",
        )
    instance = InstantiatedAgent(
        spec=spec,
        invoker_subject=subject,
        child_tags=child_tags,
        namespace=invoker_namespace(child_tags.tenant, subject),
    )

    # Step 3 — render the CLAMPED boundary (what will actually run) for the human to confirm.
    boundary = describe_instantiated(instance)
    return DraftOutcome(ok=True, boundary=boundary, instance=instance)
