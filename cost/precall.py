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


def estimate_call_cost(
    model_id: str,
    input_tokens: int,
    max_tokens: int,
    *,
    pricebook: PriceBook | None = None,
) -> float:
    """Worst-case USD for one call: input billed in full, output billed at its cap.

    `max_tokens` is the per-call output ceiling each tier carries (design §7.1), so
    this is an upper bound on the call's cost — never an under-estimate.
    """
    pb = pricebook or default_pricebook()
    rate = pb.llm_rate(model_id)
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
) -> PrecallResult:
    """Allow/reject a call before it runs, by exact worst-case projection.

    - budget None        → no cap → allow (projection still reported).
    - budget <= 0        → no allocation → reject.
    - spend + worst-case > budget → reject (this call could exceed budget).
    - otherwise          → allow.

    Fails closed on a malformed (negative) spend, mirroring the soft cap.
    """
    est = estimate_call_cost(model_id, input_tokens, max_tokens, pricebook=pricebook)
    projected = round(spend + est, 6)

    if budget is None:
        return PrecallResult("allow", est, projected, "no budget configured")
    if budget <= 0:
        return PrecallResult("reject", est, projected, "no allocation")
    if spend < 0:
        return PrecallResult("reject", est, projected, "invalid spend")
    if projected > budget:
        return PrecallResult("reject", est, projected, "would exceed budget")
    return PrecallResult("allow", est, projected, "within budget")
