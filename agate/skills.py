"""Skills — governed capability packages (#119 slice 1, vision §8.6).

Adopts the open "Skills" idea (portable, model-agnostic capability packages) for interop,
keeping agate's boundary underneath: a **Skill is a reviewed bundle of capabilities** (a
reasoning hint + a set of capability names), and listing a skill in a spec is *sugar* for
listing its capabilities. The vision §8.6 framing exactly: "`agate.patterns` is already a
proto-skill registry — generalize it to load/compose Skills; *which* skills an agent may load
is a spec field, hence IAM-governed; a Skill runs under the agent's bounded credential."

The whole safety property reduces to one rule, enforced here: **a Skill can never grant a
capability the agent couldn't have declared directly.** Every capability a skill bundles must
already exist in the #113 capability catalog, and the spec expands `skills` into the effective
`tools` set — which the #105 compiler then clamps to the agent's scope/tier exactly as for an
explicitly-listed tool. A skill that names an uncatalogued capability fails closed. So a skill
introduces NO new IAM path; it is a portable, nameable unit over already-grantable capabilities.

Pure and AWS-free, like `patterns`/`agentspec`. Capability/pattern validation is done via a
lazy import of `agentspec`/`patterns` (the spec parser imports THIS module, so the edge is
one-directional and resolved at call time, not import time).
"""

from __future__ import annotations

from dataclasses import dataclass


class SkillError(ValueError):
    """An unknown skill, a duplicate registration, or a skill that names a capability the
    catalog doesn't have. Fail closed: a skill can never invent a grant."""


@dataclass(frozen=True, slots=True)
class Skill:
    """A reviewed capability package. `capabilities` are #113 capability NAMES the skill
    bundles (each must exist in the catalog — validated by `validate_skill`). `pattern` is
    an optional `patterns` key the skill's reasoning maps to (None = the spec keeps its own
    `reasoning`). Pure metadata — WHAT a skill knows how to do + WHICH reviewed capabilities
    it needs, never raw IAM."""

    name: str
    title: str
    description: str
    capabilities: tuple[str, ...] = ()
    pattern: str | None = None


_SKILLS: dict[str, Skill] = {}


def register_skill(skill: Skill) -> Skill:
    if skill.name in _SKILLS:
        raise SkillError(f"duplicate skill: {skill.name!r}")
    _SKILLS[skill.name] = skill
    return skill


def get_skill(name: str) -> Skill:
    try:
        return _SKILLS[name]
    except KeyError as exc:
        raise SkillError(f"unknown skill: {name!r}") from exc


def skill_catalog() -> list[dict[str, str]]:
    """The selectable skills, for the §8.5 authoring UI (name/title/description)."""
    return [
        {"name": s.name, "title": s.title, "description": s.description}
        for s in _SKILLS.values()
    ]


def validate_skill(skill: Skill) -> None:
    """Fail-closed check that a skill bundles only REAL capabilities (and a real pattern, if
    set) — so it can never grant what the catalog doesn't have. Lazy imports break the cycle
    (`agentspec` imports this module). Called at spec-parse expansion and unit-tested directly."""
    from agate.agentspec import get_capability  # lazy: agentspec imports skills

    for cap in skill.capabilities:
        get_capability(cap)  # raises SpecError on an unknown capability
    if skill.pattern is not None:
        from agate.patterns import get as get_pattern  # lazy

        get_pattern(skill.pattern)  # raises PatternError on an unknown pattern


def skill_capabilities(name: str) -> tuple[str, ...]:
    """The capability names a skill expands to (validated). The spec unions these into its
    effective `tools` — so the compiler emits exactly the skill's reviewed grants, clamped to
    the agent's scope/tier, denied-by-absence for anything else."""
    skill = get_skill(name)
    validate_skill(skill)
    return skill.capabilities


# --- reference skills -------------------------------------------------------
# Compose EXISTING #113 capabilities into portable packages (mirroring how patterns.py ships
# lit-review/red-team). Each is sugar over its capabilities — no new grant. Validation is
# deferred to expansion (validate_skill), so registration here is order-independent of the
# capability catalog's module-load.

register_skill(
    Skill(
        name="lit-reviewer",
        title="Literature reviewer",
        description="Search the library + read course materials, then run a lit-review panel.",
        capabilities=("library-search", "course-materials-reader"),
        pattern="lit-review",
    )
)
register_skill(
    Skill(
        name="hpc-analyst",
        title="HPC analyst",
        description="Monitor and submit HPC jobs to the caller's own allocation.",
        capabilities=("hpc-monitor", "hpc-submit"),
    )
)
