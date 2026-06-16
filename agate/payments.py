"""Agent payments — the governance core (#120, vision §8.6).

Turns agate from "governs what an agent can *read and run*" into "…read, run, **and pay
for**" — with the SAME single boundary. The unsolved problem in agent payments is *bounded
autonomy*: let an agent spend, but never too much, on the wrong dime, or beyond its share.
agate already solves this for model tokens (authoritative metering #79, pre-call checks, the
hierarchical cascade #81, monotonic delegation #106). #120 generalizes "spend" from tokens to
ANY priced action:

  * **AP2** — a spec's compiled `budget` becomes a scoped, delegatable spending **Mandate**;
    a child agent's mandate is provably ⊆ its parent's (`delegate.delegate_budget`).
  * **x402** — a flat-priced (HTTP-402) tool/data call is **pre-authorized against remaining
    budget before it fires** (`cost.evaluate_priced_call`/`evaluate_priced_cascade`, the
    chokepoint pattern), then attributed per-hop.

This module is PURE and AWS-free. It adds NO new trust surface: the Mandate is the compiled
`budget` (already scope/tier-bounded by #105) narrowed by the same #106 rule as any delegated
authority; the gate is the same `_node_decision` the token chokepoint uses; the attribution is
the same #137 `ActingAs`. The vendor-quoted price is used ONLY to gate + debit — the budget
ceiling, never the price, is the authority. The x402 wire and the AP2 signature/JWS transport
live in **agenkit** (§0.1: agenkit owns protocol mechanics; agate owns the authority under
them); the live debit reuses the existing `meter` (deferred, like #115/#136).
"""

from __future__ import annotations

from dataclasses import dataclass

from cost.precall import PrecallResult, evaluate_priced_call

from agate.agentspec import BudgetSpec
from agate.delegate import delegate_budget, scope_intersect
from agate.identity import ActingAs


class PaymentError(ValueError):
    """A spend authorization that cannot be formed safely — a child mandate disjoint from its
    parent's scope, or a malformed amount. Fail closed: refuse rather than over-authorize."""


@dataclass(frozen=True, slots=True)
class Mandate:
    """A scoped, delegatable spending authorization — the AP2 concept as agate data (the
    signature/wire is agenkit's, deferred). It is the compiled `budget` bound to a verified
    `(tenant, scope, subject)`: `limit_usd` is the ceiling, `period` the window, and
    `acting_as` (#137) names WHO may spend on WHOSE authority within WHAT remit — so every
    priced action under it is attributable. A child mandate is `delegate_mandate`d from its
    parent and is provably ⊆ it."""

    tenant: str
    scope: str
    subject: str
    limit_usd: float
    period: str
    acting_as: ActingAs

    def to_dict(self) -> dict:
        return {
            "tenant": self.tenant,
            "scope": self.scope,
            "subject": self.subject,
            "limit_usd": self.limit_usd,
            "period": self.period,
            "acting_as": self.acting_as.to_dict(),
        }


def mandate_from_budget(
    compiled, *, tenant: str, subject: str, scope: str | None = None
) -> Mandate | None:
    """Build the spending Mandate for a compiled agent run by a VERIFIED `(tenant, subject)`.
    Returns None when the spec declares NO budget — no budget means no spending authority
    (an agent can't pay for anything unless its author granted a ceiling). The mandate's
    `acting_as` is the SAME #137 record the agent's other actions carry (recovered from the
    verified session, never client-forged), so a payment is attributed identically to a
    model call. `scope` defaults to the compiled agent's own scope (its remit)."""
    budget: BudgetSpec | None = compiled.spec.budget
    if budget is None:
        return None
    from agate.agentcompile import acting_as  # lazy: agentcompile imports nothing from here

    record = acting_as(compiled, session_name=_session_name(tenant, subject))
    bound_scope = scope if scope is not None else compiled.spec.scope
    return Mandate(
        tenant=tenant,
        scope=bound_scope,
        subject=subject,
        limit_usd=float(budget.usd),
        period=budget.period_kind,
        acting_as=record,
    )


def _session_name(tenant: str, subject: str) -> str:
    from agate.tags import role_session_name  # lazy, keep the import graph shallow

    return role_session_name(tenant, subject)


def delegate_mandate(
    parent: Mandate,
    child_budget: BudgetSpec | None,
    *,
    child_subject: str,
    child_scope: str,
    child_acting_as: ActingAs,
) -> Mandate:
    """The child's Mandate, NARROWED from its parent — the headline invariant (§2/§8.6).

    `limit_usd = delegate_budget(parent.limit_usd, child_budget)` reuses the EXACT #106 rule:
    a slice of the parent's ceiling, capped by the child's own ask — so the child can never
    out-spend the parent, and "my research agent may buy datasets up to $50/mo" cannot become
    "and so may every sub-agent it spawns, each up to $50." Scope is
    `scope_intersect(parent.scope, child_scope)`; a disjoint child scope raises `PaymentError`
    (fail-closed — never widen). Transitive: a grandchild narrows from the child, which
    narrowed from the parent."""
    inter = scope_intersect(parent.scope, child_scope)
    if inter is None:
        raise PaymentError(
            f"child scope {child_scope!r} is disjoint from the parent mandate's scope "
            f"{parent.scope!r} — refusing to delegate spending authority"
        )
    limit = delegate_budget(parent.limit_usd, child_budget)
    # delegate_budget returns None only when BOTH sides are uncapped; a parent mandate always
    # carries a finite limit, so `limit` is finite here. Guard anyway (fail-closed to 0).
    if limit is None:
        limit = parent.limit_usd
    return Mandate(
        tenant=parent.tenant,
        scope=inter,
        subject=child_subject,
        limit_usd=float(limit),
        period=parent.period,
        acting_as=child_acting_as,
    )


def authorize_spend(
    mandate: Mandate, *, price_usd: float, spend_so_far: float
) -> PrecallResult:
    """The spend gate for ONE priced action under a mandate (the chokepoint pattern for a
    priced call). A thin wrapper over `cost.evaluate_priced_call` against the mandate's
    `limit_usd`: an action whose price would push `spend_so_far` over the ceiling is rejected
    — regardless of how the vendor quoted the price, because the ceiling is the authority. A
    negative price fails closed."""
    return evaluate_priced_call(
        price_usd=price_usd, spend=spend_so_far, budget=mandate.limit_usd
    )


def priced_action_row(
    mandate: Mandate, *, price_usd: float, label: str, vendor: str = ""
) -> dict:
    """The receipt/spend row for a SETTLED priced call — recorded + attributed exactly like a
    model call (mirrors the meter's `CostRow`/receipt shape). `kind="x402"` marks it a priced
    action; `actingAs` is the mandate's #137 attribution (who · on whose authority · remit),
    so the audit shows who paid what to which vendor under whose mandate. The live debit (the
    executor calls the existing `meter` increment + the graph/room cascade-debit loop) is
    deferred like #115/#136."""
    return {
        "label": label,
        "kind": "x402",
        "cost": round(float(price_usd), 6),
        "vendor": vendor,
        "actingAs": mandate.acting_as.to_dict(),
    }
