"""CostMeter (design §7.2, §13.6) — actual dollars per call, itemised receipt.

Pure and side-effect-free: USD is computed from authoritative usage (the token
counts the model returns, or logged counts server-side) × the resolved rates. The
same engine costs LLM calls, embeddings, retrieval (per-1k queries), and compute
(Code Interpreter seconds). It emits an itemised `Receipt` (rows + total) that
doubles as per-user/per-tenant chargeback.

Thread-safe so a parallel Panel fan-out can meter concurrently. Satisfies the
`CostMeter` protocol the orchestration (agg.panel / agg.analyze / agg.router) calls:
`total`, `add_llm`, `add_compute`. Adds `add_embedding` / `add_retrieval` for the
ingest and RAG paths.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

from cost.pricing import PriceBook, default_pricebook

CostKind = Literal["llm", "embedding", "retrieval", "compute"]


@dataclass(frozen=True, slots=True)
class CostRow:
    """One itemised line: what it was, and the dollars it cost."""

    label: str
    kind: CostKind
    cost: float
    # Optional provenance for the receipt / chargeback.
    model_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Receipt:
    """The closed run total + its itemised rows."""

    rows: list[CostRow]
    total: float

    def to_event(self) -> dict:
        """The `receipt` event payload (§10.2.9) — rows + total for the SPA."""
        return {
            "type": "receipt",
            "rows": [
                {"label": r.label, "kind": r.kind, "cost": round(r.cost, 6)} for r in self.rows
            ],
            "total": round(self.total, 6),
        }


def _round6(x: float) -> float:
    return round(x, 6)


class CostMeter:
    """Accumulates itemised cost rows and a running total. Thread-safe."""

    def __init__(self, pricebook: PriceBook | None = None):
        self._pricebook = pricebook or default_pricebook()
        self._rows: list[CostRow] = []
        self._total = 0.0
        self._lock = threading.Lock()

    @property
    def total(self) -> float:
        return self._total

    @property
    def rows(self) -> list[CostRow]:
        with self._lock:
            return list(self._rows)

    def _add(self, row: CostRow) -> float:
        with self._lock:
            self._rows.append(row)
            self._total += row.cost
        return row.cost

    def add_llm(self, label: str, tier: str, model_label: str, usage: dict[str, int]) -> float:
        """Cost an LLM call: input/output tokens × per-million rates.

        `tier`/`model_label` carry the logical id the roster uses; the PriceBook
        resolves it to a rate (config override → hard default). Returns the dollars.
        """
        rate = self._pricebook.llm_rate(tier)
        in_tok = usage.get("inputTokens", 0)
        out_tok = usage.get("outputTokens", 0)
        cost = (in_tok / 1e6) * rate.input_per_mtok + (out_tok / 1e6) * rate.output_per_mtok
        return self._add(
            CostRow(
                label=label,
                kind="llm",
                cost=_round6(cost),
                model_id=model_label,
                input_tokens=in_tok,
                output_tokens=out_tok,
            )
        )

    def add_embedding(self, label: str, model_id: str, input_tokens: int) -> float:
        """Cost an embedding call: input tokens × per-million rate."""
        rate = self._pricebook.llm_rate(model_id)
        cost = (input_tokens / 1e6) * rate.input_per_mtok
        return self._add(
            CostRow(
                label=label,
                kind="embedding",
                cost=_round6(cost),
                model_id=model_id,
                input_tokens=input_tokens,
            )
        )

    def add_retrieval(self, label: str, queries: int = 1) -> float:
        """Cost retrieval: per-1k-queries rate."""
        cost = (queries / 1000.0) * self._pricebook.retrieval_rate_per_k()
        return self._add(CostRow(label=label, kind="retrieval", cost=_round6(cost)))

    def add_compute(self, label: str, seconds: float) -> float:
        """Cost compute (Code Interpreter): per-second rate."""
        cost = seconds * self._pricebook.compute_rate_per_sec()
        return self._add(CostRow(label=label, kind="compute", cost=_round6(cost)))

    def receipt(self) -> Receipt:
        """Snapshot the itemised receipt (rows + total)."""
        with self._lock:
            return Receipt(rows=list(self._rows), total=_round6(self._total))
