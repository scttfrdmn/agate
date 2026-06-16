"""Exact pre-call budget enforcement (design §7.1, Tier 1).

The soft cap (cost.softcap) declines to START the next call once accumulated spend
crosses budget — bounded overrun, no in-flight kill. Tier 1 institutions want
EXACT pre-call enforcement: reject a call when its worst-case cost would push the
user over budget *before* it runs. That is this module — a pure decision over the
call's token ceiling × rate plus the authoritative spend already recorded.

Worst-case cost = input_tokens × input_rate + max_tokens × output_rate (the call
can't emit more than max_tokens). This never under-estimates, so a call that the
gate allows cannot, by itself, exceed budget.

Pure and AWS-free; the chokepoint Lambda supplies the authoritative spend (from the
spend table) and the budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cost.pricing import PriceBook, default_pricebook

PrecallDecision = Literal["allow", "reject"]


@dataclass(frozen=True, slots=True)
class PrecallResult:
    decision: PrecallDecision
    estimated_cost: float  # worst-case USD for this call
    projected_total: float  # spend + estimated_cost
    reason: str


@dataclass(frozen=True, slots=True)
class CascadeResult:
    """Outcome of a multi-node (hierarchical-budget) pre-call check (#81)."""

    decision: PrecallDecision
    estimated_cost: float  # worst-case USD for this call (priced once)
    breaching_node: str | None  # label of the FIRST node that rejected, or None
    reason: str


def _node_decision(est: float, spend: float, budget: float | None) -> tuple[bool, str]:
    """The per-node allow/reject rule, shared by the single- and multi-budget gates.

    Mirrors the original evaluate_precall branches exactly so behaviour can't drift:
    no budget -> ok (no cap); <=0 -> reject; negative spend -> reject (fail closed);
    spend + worst-case > budget -> reject; else ok.
    """
    if budget is None:
        return True, "no budget configured"
    if budget <= 0:
        return False, "no allocation"
    if spend < 0:
        return False, "invalid spend"
    if round(spend + est, 6) > budget:
        return False, "would exceed budget"
    return True, "within budget"


def estimate_call_cost(
    model_id: str,
    input_tokens: int,
    max_tokens: int,
    *,
    pricebook: PriceBook | None = None,
    fallback_tier: str | None = None,
) -> float:
    """Worst-case USD for one call: input billed in full, output billed at its cap.

    `max_tokens` is the per-call output ceiling each tier carries (design §7.1), so
    this is an upper bound on the call's cost — never an under-estimate. `fallback_tier`
    is passed to `llm_rate` so an unlisted concrete id prices at its tier (#88).
    """
    pb = pricebook or default_pricebook()
    rate = pb.llm_rate(model_id, fallback_tier=fallback_tier)
    return round(
        (max(0, input_tokens) / 1e6) * rate.input_per_mtok
        + (max(0, max_tokens) / 1e6) * rate.output_per_mtok,
        6,
    )


def evaluate_precall(
    *,
    model_id: str,
    input_tokens: int,
    max_tokens: int,
    spend: float,
    budget: float | None,
    pricebook: PriceBook | None = None,
    fallback_tier: str | None = None,
) -> PrecallResult:
    """Allow/reject a call before it runs, by exact worst-case projection.

    - budget None        → no cap → allow (projection still reported).
    - budget <= 0        → no allocation → reject.
    - spend + worst-case > budget → reject (this call could exceed budget).
    - otherwise          → allow.

    Fails closed on a malformed (negative) spend, mirroring the soft cap.
    """
    est = estimate_call_cost(
        model_id, input_tokens, max_tokens, pricebook=pricebook, fallback_tier=fallback_tier
    )
    projected = round(spend + est, 6)
    ok, reason = _node_decision(est, spend, budget)
    return PrecallResult("allow" if ok else "reject", est, projected, reason)


def evaluate_cascade(
    *,
    model_id: str,
    input_tokens: int,
    max_tokens: int,
    nodes: list[tuple[str, float, float | None]],
    pricebook: PriceBook | None = None,
    fallback_tier: str | None = None,
) -> CascadeResult:
    """Allow a call only if it fits under EVERY node's budget (hierarchical cascade).

    `nodes` is an ordered list of `(label, spend, budget)` — typically the user/tenant
    node followed by each scope ancestor (broad -> specific). The call is priced ONCE
    (worst case) and checked against each node with the same per-node rule as
    `evaluate_precall`; the FIRST node to reject short-circuits and is named. A node
    with `budget is None` is skipped (no cap there). An empty `nodes` list -> allow
    (e.g. an unconfined session with no caps). Pure and AWS-free — the chokepoint
    supplies each node's authoritative spend + budget.
    """
    est = estimate_call_cost(
        model_id, input_tokens, max_tokens, pricebook=pricebook, fallback_tier=fallback_tier
    )
    for label, spend, budget in nodes:
        ok, reason = _node_decision(est, spend, budget)
        if not ok:
            return CascadeResult("reject", est, label, reason)
    return CascadeResult("allow", est, None, "within budget")


# --- flat-USD priced actions (#120) -----------------------------------------
# An x402-priced tool/data call is "another metered action": instead of pricing tokens, the
# (vendor-quoted, caller-supplied) flat USD price IS the worst-case estimate. These gates
# reuse the SAME `_node_decision` rule as the token gates above, so there is no behavioural
# drift — the budget ceiling is the authority, never the quoted price. A negative price fails
# closed (mirrors the negative-spend guard). agate gates + debits; the x402 wire is agenkit's.


def evaluate_priced_call(
    *, price_usd: float, spend: float, budget: float | None
) -> PrecallResult:
    """Allow/reject ONE flat-priced action before it fires (the chokepoint pattern, for a
    priced call instead of a model call). `price_usd` is the worst-case cost; a negative
    price is rejected. Same allow/reject rule as `evaluate_precall`."""
    if price_usd < 0:
        return PrecallResult("reject", price_usd, round(spend, 6), "invalid price")
    projected = round(spend + price_usd, 6)
    ok, reason = _node_decision(price_usd, spend, budget)
    return PrecallResult("allow" if ok else "reject", price_usd, projected, reason)


def evaluate_priced_cascade(
    *, price_usd: float, nodes: list[tuple[str, float, float | None]]
) -> CascadeResult:
    """Allow a flat-priced action only if it fits under EVERY node's budget (the hierarchical
    cascade, #81/#112 — so a runaway sub-agent can't drain the family ceiling). Identical loop
    to `evaluate_cascade`, with the quoted `price_usd` as the estimate. A negative price fails
    closed; the FIRST breaching node is named."""
    if price_usd < 0:
        return CascadeResult("reject", price_usd, None, "invalid price")
    for label, spend, budget in nodes:
        ok, reason = _node_decision(price_usd, spend, budget)
        if not ok:
            return CascadeResult("reject", price_usd, label, reason)
    return CascadeResult("allow", price_usd, None, "within budget")
