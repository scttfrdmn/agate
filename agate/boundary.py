"""Effective-boundary view — what an agent can actually touch / do / spend (#108, §8.5).

#105/#106/#107 made an agent's authority a GENERATED artifact (a `CompiledAgent`, and an
`InstantiatedAgent` for a specific invoker). The classic IAM tragedy is that nobody knows
what a policy actually grants — so this module renders that authority in PLAIN LANGUAGE:
every model the agent may invoke, the data path it can read, the tools it can use, and
its spend ceiling, plus the explicit denials (what it CANNOT do — half the value of a
legible bound). It is the trust surface behind graphical authoring (§8.5) and the
human-facing complement to the `iam:SimulateCustomPolicy` proofs.

It is PURE and derives the summary from the SAME compiled artifacts the credential is
built from — `tags_template.tier`/`.scope`, `spec.tools`, `spec.budget` — never a second,
independently-derived source that could drift. A live drift proof
(`tests/test_proof_boundary.py`) asserts every ALLOW the view claims is `allowed` in IAM
and every DENIAL is denied — so the explanation can never disagree with enforcement.

Security property: the view must never UNDER-state the boundary. Every grant the agent
actually holds must appear, so an admin trusting the view is never surprised by an
omitted capability. The proof checks both directions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agate.agentspec import AgentSpec, get_capability
from agate.entitlements import TIER_RANK, TIERS, Tier, models_for_tier

ItemKind = Literal["model", "data", "tool", "spend"]


@dataclass(frozen=True, slots=True)
class BoundaryItem:
    """One human-readable line of the boundary. `allow=False` lines are the explicit
    'cannot' statements that make the bound legible."""

    kind: ItemKind
    allow: bool
    detail: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "allow": self.allow, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class EffectiveBoundary:
    """The plain-language authority of an agent. `allows` is what it CAN do; `denials`
    the explicit 'cannot' lines. JSON-serialisable for the admin API / authoring UI."""

    agent_name: str
    tier: Tier
    scope: str  # "" == tenant-wide
    allows: tuple[BoundaryItem, ...]
    denials: tuple[BoundaryItem, ...]
    subject: str = ""  # set for a per-invoker (InstantiatedAgent) boundary

    def summary(self) -> list[str]:
        """Plain-English lines a non-expert reads. Allows first, then the 'cannot' lines."""
        who = f"Agent {self.agent_name!r}" + (f" (for {self.subject})" if self.subject else "")
        lines = [f"{who} — effective boundary:"]
        lines += [f"  CAN  {i.detail}" for i in self.allows]
        lines += [f"  CANNOT {i.detail}" for i in self.denials]
        return lines

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "tier": self.tier,
            "scope": self.scope,
            "subject": self.subject,
            "allows": [i.to_dict() for i in self.allows],
            "denials": [i.to_dict() for i in self.denials],
        }


def _model_lines(tier: Tier) -> tuple[list[BoundaryItem], list[BoundaryItem]]:
    """What models the tier may invoke, + the explicit higher-tier denial."""
    models = models_for_tier(tier)
    allows = [
        BoundaryItem("model", True, f"invoke {tier}-tier models: {', '.join(models)}"),
    ]
    denials: list[BoundaryItem] = []
    higher = [t for t in TIERS if TIER_RANK[t] > TIER_RANK[tier]]
    if higher:
        denials.append(
            BoundaryItem("model", False, f"invoke higher-tier models ({', '.join(higher)})")
        )
    return allows, denials


def _data_lines(scope: str) -> tuple[list[BoundaryItem], list[BoundaryItem]]:
    """What documents the agent can read, + the subtree-confinement denial (#80)."""
    if not scope:
        return (
            [BoundaryItem("data", True, "read documents tenant-wide (no scope confinement)")],
            [BoundaryItem("data", False, "read another tenant's documents")],
        )
    return (
        [BoundaryItem("data", True, f"read documents under {{tenant}}/{scope}/ only")],
        [
            BoundaryItem(
                "data",
                False,
                f"read documents outside {{tenant}}/{scope}/ "
                "(sibling or parent subtrees, or another tenant)",
            ),
        ],
    )


def _tool_lines(spec: AgentSpec) -> tuple[list[BoundaryItem], list[BoundaryItem]]:
    """The declared tools (read vs draft-write), + the denied-by-absence headline."""
    allows: list[BoundaryItem] = []
    for name in spec.tools:
        cap = get_capability(name)
        mode = "write (to a draft queue, never live)" if cap.grant.write else "read-only"
        allows.append(BoundaryItem("tool", True, f"use tool '{cap.title}' — {mode}"))
    # The headline: anything not listed is denied (tools are denied by absence).
    denials = [BoundaryItem("tool", False, "use any tool not listed above (denied by absence)")]
    return allows, denials


def _spend_lines(spec: AgentSpec) -> list[BoundaryItem]:
    b = spec.budget
    if b is None:
        return [BoundaryItem("spend", True, "no budget ceiling declared in the spec")]
    return [
        BoundaryItem("spend", True, f"spend up to ${b.usd:g} per {b.per} per {b.period_kind}"),
    ]


def _describe(
    name: str, tier: Tier, scope: str, spec: AgentSpec, *, subject: str = ""
) -> EffectiveBoundary:
    m_allow, m_deny = _model_lines(tier)
    d_allow, d_deny = _data_lines(scope)
    t_allow, t_deny = _tool_lines(spec)
    return EffectiveBoundary(
        agent_name=name,
        tier=tier,
        scope=scope,
        allows=tuple(m_allow + d_allow + t_allow + _spend_lines(spec)),
        denials=tuple(m_deny + d_deny + t_deny),
        subject=subject,
    )


def describe(compiled) -> EffectiveBoundary:
    """The effective boundary of a `CompiledAgent` — read from its compiled artifacts
    (tags_template.tier/scope + spec), the SAME inputs its policies were built from, so
    the view cannot drift from enforcement. (Typed loosely to avoid an import cycle with
    agentcompile; it expects a `CompiledAgent`.)"""
    tags = compiled.tags_template
    return _describe(compiled.spec.name, tags.tier, tags.scope, compiled.spec)


def describe_instantiated(inst) -> EffectiveBoundary:
    """The CONCRETE per-invoker boundary of an `InstantiatedAgent`: the actual narrowed
    tier/scope this specific invoker's instance carries (`inst.child_tags`), with the
    invoker named. Shows exactly what one student's instance of a shared agent can do."""
    tags = inst.child_tags
    return _describe(inst.spec.name, tags.tier, tags.scope, inst.spec, subject=inst.invoker_subject)
