"""Soft cap (design §7.1) — the pure spend-vs-budget decision.

You never kill a call in flight; you decline to START the next one. The broker reads
AUTHORITATIVE spend (computed server-side from invocation logs × rates, never
client-reported) at each credential issue/refresh and decides whether to vend model
credentials. Over budget → vend no model creds (or read-only); because creds are
short-lived, an over-budget user loses model access at the next refresh.

This is the pure decision, AWS-free and unit-tested. The broker supplies the
authoritative spend (from the `spend` table) and the budget; the meter/pricing
engine produces the spend number elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CapDecision = Literal["allow", "deny"]


@dataclass(frozen=True, slots=True)
class CapResult:
    decision: CapDecision
    # Fraction of budget consumed (0.0–∞); useful for a UI warning band.
    utilisation: float
    reason: str


def evaluate_soft_cap(
    spend: float,
    budget: float | None,
    *,
    warn_at: float = 0.9,
) -> CapResult:
    """Decide whether to vend model credentials given spend vs budget.

    - budget is None  → no cap configured → always allow (utilisation 0).
    - budget <= 0     → a zero/negative budget denies (explicitly no allocation).
    - spend >= budget → deny (over budget; vend no model creds at this refresh).
    - else            → allow, flagging when utilisation crosses `warn_at`.

    Fails closed on a malformed (negative) spend by treating it as over budget,
    since a negative authoritative spend should never happen and must not widen access.
    """
    if budget is None:
        return CapResult("allow", 0.0, "no budget configured")
    if budget <= 0:
        return CapResult("deny", float("inf"), "no allocation")
    if spend < 0:
        return CapResult("deny", float("inf"), "invalid spend")

    utilisation = spend / budget
    if spend >= budget:
        return CapResult("deny", utilisation, "over budget")
    if utilisation >= warn_at:
        return CapResult("allow", utilisation, "approaching budget")
    return CapResult("allow", utilisation, "within budget")
