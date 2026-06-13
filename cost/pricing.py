"""Pricing (design §7.2, §13.6) — rates with config + hard-default fallbacks.

A `PriceBook` maps a model/service id to its rates. Resolution order:
  1. an explicit config override (institution-supplied),
  2. the live AWS Price List API (optional edge, not loaded here),
  3. a hard-coded default below — so the meter ALWAYS returns a number and never
     blocks a call on a missing rate.

Price List quirks respected (verified against the design doc, 2026-05):
  * S3 Vectors is not yet in the Price List → config/hard-default only.
  * Claude 4.x prices live under `AmazonBedrockFoundationModels`, not `AmazonBedrock`.
  * The Price List API is us-east-1 only; usagetype prefixes are regional.

Rates are USD per MILLION tokens (LLM/embeddings), per 1k queries (retrieval), or
per second (compute). Hard defaults are deliberately conservative placeholders,
neutral (keyed by logical id, no product names); override them with real rates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Pricing kinds and their unit semantics.
# - llm:        (input_per_mtok, output_per_mtok) USD per 1e6 tokens
# - embedding:  input_per_mtok USD per 1e6 tokens (no output)
# - retrieval:  per_thousand USD per 1000 queries
# - compute:    per_second USD per wall-clock second


@dataclass(frozen=True, slots=True)
class ModelRate:
    """Per-million-token rates for an LLM or embedding model."""

    input_per_mtok: float
    output_per_mtok: float = 0.0


# Hard-default LLM/embedding rates, keyed by the logical tier/id the roster uses
# (neutral labels — institutions map these to concrete model ids + real rates).
# Values are conservative placeholders in USD per million tokens.
_DEFAULT_MODEL_RATES: dict[str, ModelRate] = {
    "oss": ModelRate(input_per_mtok=0.10, output_per_mtok=0.40),
    "mid": ModelRate(input_per_mtok=0.80, output_per_mtok=4.00),
    "frontier": ModelRate(input_per_mtok=3.00, output_per_mtok=15.00),
    "router": ModelRate(input_per_mtok=0.10, output_per_mtok=0.40),
    # Embedding models (output rate unused).
    "embed-text": ModelRate(input_per_mtok=0.02),
    "embed-multimodal": ModelRate(input_per_mtok=0.06),
}

# Retrieval (per 1000 queries) and compute (per second) hard defaults. S3 Vectors
# query pricing comes from config (not in the Price List) — this is the fallback.
_DEFAULT_RETRIEVAL_PER_K = 0.25
_DEFAULT_COMPUTE_PER_SEC = 0.0002


@dataclass(slots=True)
class PriceBook:
    """Resolves rates with config-override-then-hard-default fallbacks."""

    model_rates: dict[str, ModelRate] = field(default_factory=dict)
    retrieval_per_k: float | None = None
    compute_per_sec: float | None = None

    def llm_rate(self, model_id: str) -> ModelRate:
        """Rate for an LLM/embedding id. Config override wins; else hard default;
        else the cheapest default so an unknown id never blocks a call."""
        if model_id in self.model_rates:
            return self.model_rates[model_id]
        if model_id in _DEFAULT_MODEL_RATES:
            return _DEFAULT_MODEL_RATES[model_id]
        # Unknown id: fall back to the oss-tier default (cheapest), never crash.
        return _DEFAULT_MODEL_RATES["oss"]

    def retrieval_rate_per_k(self) -> float:
        return (
            self.retrieval_per_k if self.retrieval_per_k is not None else _DEFAULT_RETRIEVAL_PER_K
        )

    def compute_rate_per_sec(self) -> float:
        return (
            self.compute_per_sec if self.compute_per_sec is not None else _DEFAULT_COMPUTE_PER_SEC
        )


def default_pricebook() -> PriceBook:
    """A PriceBook backed entirely by hard defaults (no config, no live fetch)."""
    return PriceBook()
