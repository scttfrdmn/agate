"""Composable reasoning patterns (Phase 9 Track 2, #64).

The "do better" axis: Panel and Analyze proved that structured reasoning beats plain
chat. This turns reasoning constructs into **institution-composed declarative
configs** rather than hard-coded modes — a pattern names a set of *roles* (each a
label + a system prompt + a model preference) over the EXISTING orchestration
primitives (`agate.panel` / `agate.analyze`, driven by `agate.agent_dispatch`).

Deliberately simple (per the Phase 9 decision): a pattern is reviewed config loaded
from a registry. There is NO DSL and NO end-user runtime builder — an institution
adds a Pattern here (or, later, a reviewed registry file) and it becomes selectable.

Pure + AWS-free. A pattern names model PREFERENCES (cheapest/best/balanced/by-index),
never concrete model ids, so it stays neutral and is materialised against whatever
models the verified caller is ENTITLED to at run time (`compile_pattern`). The output
is an ordinary dispatch payload, so `agate.agent_dispatch.dispatch` runs it unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# A role's model preference, resolved against the caller's entitled model list
# (ordered cheapest-first, as `agate.entitlements.models_for_tier` returns).
ModelPref = Literal["cheapest", "best", "balanced"]


@dataclass(frozen=True, slots=True)
class Role:
    """One participant in a pattern: a labelled reviewer with its own system prompt
    and a model preference. `label` is the pane label the SPA renders."""

    label: str
    system: str
    model: ModelPref = "balanced"
    max_tokens: int = 1024


@dataclass(frozen=True, slots=True)
class Pattern:
    """A reviewed reasoning construct that composes the existing primitives.

    `mode` selects the orchestration (DEBATE = the multi-role panel, the richest;
    SYNTHESIS = single grounded answer; ANALYSIS = code path). `roles` are the panel
    members; `adjudicator` reconciles them (DEBATE only). The system prompts are the
    institution's reasoning recipe — e.g. "always include a citation-checker".
    """

    key: str
    title: str
    description: str
    mode: Literal["SYNTHESIS", "DEBATE", "ANALYSIS"]
    roles: tuple[Role, ...] = ()
    adjudicator: Role | None = None
    # An optional shared review-system preamble prepended to every role's prompt.
    review_system: str | None = None


class PatternError(ValueError):
    """Unknown pattern key, or a pattern that cannot be compiled for this caller."""


def _pick(models: list[str], pref: ModelPref) -> str:
    """Resolve a model preference against the entitled list (cheapest-first).

    `cheapest` -> first; `best` -> last (highest tier the caller has); `balanced` ->
    the middle. Always returns an ENTITLED id, so dispatch's allowed-models check can
    never reject a pattern-chosen model for an entitled caller.
    """
    if not models:
        raise PatternError("no entitled models to compile the pattern against")
    if pref == "cheapest":
        return models[0]
    if pref == "best":
        return models[-1]
    return models[len(models) // 2]


def compile_pattern(
    pattern: Pattern,
    *,
    question: str,
    entitled_models: list[str],
    evidence: str = "",
) -> dict[str, Any]:
    """Materialise a pattern into a dispatch payload for the verified caller.

    Each role's model preference is resolved to a concrete entitled model id; the
    result is an ordinary `agate.agent_dispatch.dispatch` payload (mode + roster +
    adjudicator + evidence), so the orchestration runs unchanged. Roles that share a
    resolved model id are fine — panes are keyed by label, not model.
    """
    payload: dict[str, Any] = {"question": question, "mode": pattern.mode, "evidence": evidence}

    if pattern.mode == "DEBATE":
        if not pattern.roles:
            raise PatternError(f"pattern {pattern.key!r} is DEBATE but defines no roles")
        payload["roster"] = [
            {
                "tier": _pick(entitled_models, r.model),
                "label": r.label,
                "max_tokens": r.max_tokens,
                # Per-role system prompt — the institution's reasoning recipe.
                "system": r.system,
            }
            for r in pattern.roles
        ]
        adj = pattern.adjudicator or Role(
            label="adjudicator", system="Reconcile the reviews into a cited synthesis."
        )
        payload["adjudicator"] = {
            "tier": _pick(entitled_models, adj.model),
            "label": adj.label,
            "max_tokens": adj.max_tokens,
        }
        if pattern.review_system:
            payload["review_system"] = pattern.review_system

    elif pattern.mode == "SYNTHESIS":
        gen = pattern.roles[0] if pattern.roles else Role(label="ask", system="")
        payload["generator"] = {
            "tier": _pick(entitled_models, gen.model),
            "label": gen.label,
            "max_tokens": gen.max_tokens,
        }

    elif pattern.mode == "ANALYSIS":
        gen = pattern.roles[0] if pattern.roles else Role(label="codegen", system="", model="best")
        payload["generator"] = {
            "tier": _pick(entitled_models, gen.model),
            "label": gen.label,
            "max_tokens": gen.max_tokens,
        }

    return payload


# --- The built-in pattern registry ------------------------------------------
# Institution-composed reasoning constructs. Each composes the existing primitives;
# adding one here makes it selectable. (A reviewed registry FILE is a later option.)

_REGISTRY: dict[str, Pattern] = {}


def register(pattern: Pattern) -> Pattern:
    if pattern.key in _REGISTRY:
        raise PatternError(f"duplicate pattern key: {pattern.key!r}")
    _REGISTRY[pattern.key] = pattern
    return pattern


def get(key: str) -> Pattern:
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise PatternError(f"unknown pattern: {key!r}") from exc


def catalog() -> list[dict[str, str]]:
    """The selectable patterns, for the SPA's pattern picker (key/title/description)."""
    return [
        {"key": p.key, "title": p.title, "description": p.description, "mode": p.mode}
        for p in _REGISTRY.values()
    ]


# Two reference patterns beyond the bare Panel/Analyze modes, proving composition.

register(
    Pattern(
        key="lit-review",
        title="Literature synthesis",
        description="Three readers extract claims, methods, and gaps from the evidence; "
        "an adjudicator reconciles into a cited synthesis.",
        mode="DEBATE",
        roles=(
            Role(
                label="claims",
                model="balanced",
                system="You are a careful reader. Extract the key empirical CLAIMS the "
                "evidence makes, each with its supporting citation. Do not editorialise.",
            ),
            Role(
                label="methods",
                model="balanced",
                system="You are a methodologist. Summarise the METHODS and note any "
                "limitations or threats to validity. Cite specifics.",
            ),
            Role(
                label="gaps",
                model="best",
                system="You are a skeptical reviewer. Identify GAPS, contradictions, and "
                "unsupported leaps across the evidence. Flag anything that needs verification.",
            ),
        ),
        adjudicator=Role(
            label="synthesis",
            model="best",
            system="Reconcile the three reviews into a structured, cited synthesis. "
            "Separate well-supported findings from contested or unsupported ones.",
        ),
    )
)

register(
    Pattern(
        key="red-team",
        title="Steel-man / red-team",
        description="One model argues the strongest case FOR, one the strongest case "
        "AGAINST; an adjudicator weighs them and states what would change the verdict.",
        mode="DEBATE",
        roles=(
            Role(
                label="for",
                model="best",
                system="Argue the STRONGEST honest case FOR the proposition in the question, "
                "grounded only in the evidence. Steel-man it.",
            ),
            Role(
                label="against",
                model="best",
                system="Argue the STRONGEST honest case AGAINST the proposition, grounded "
                "only in the evidence. Steel-man the opposition.",
            ),
        ),
        adjudicator=Role(
            label="verdict",
            model="best",
            system="Weigh the two cases. State which is better supported by the evidence "
            "and explicitly name what new evidence would change the verdict.",
        ),
    )
)
