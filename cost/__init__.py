"""Cost engine (design §7.2, §13.6) — pure, side-effect-free dollar metering.

`CostMeter` computes ACTUAL dollars per call from authoritative usage × live rates,
itemised into a receipt. It is the same engine for LLM calls, embeddings/ingestion,
retrieval, and compute (Code Interpreter seconds). Kept pure and AWS-free so it is
unit-testable without boto3; rate sourcing (`pricing`) has config + hard-default
fallbacks and an optional live Price List fetch at the edge.
"""

from cost.meter import CostMeter, CostRow, Receipt
from cost.pricing import (
    ModelRate,
    PriceBook,
    default_pricebook,
)
from cost.softcap import CapResult, evaluate_soft_cap

__all__ = [
    "CapResult",
    "CostMeter",
    "CostRow",
    "ModelRate",
    "PriceBook",
    "Receipt",
    "default_pricebook",
    "evaluate_soft_cap",
]
