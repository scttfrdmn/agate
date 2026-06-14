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


# Hard-default LLM/embedding rates in USD per MILLION tokens. Two key kinds:
#   * concrete Bedrock model ids (what the meter/chokepoint actually pass) — the
#     authoritative pricing path; these must be present or pricing silently falls
#     back (the #88 bug: every real id fell through to the cheapest "oss" rate).
#   * logical tier labels (oss/mid/frontier) — the FALLBACK rung for an id that
#     isn't individually listed (a session's tier is known to the caller, which
#     passes it as `fallback_tier`).
#
# RATE VALUES are best-effort published Bedrock list prices as of 2026-06; they are
# NOT fetched live and NOT authoritative — verify against the Bedrock console and
# update as AWS prices change. A deploy-time Price List fetcher (separate issue)
# will replace these with real numbers. Config overrides (PriceBook.model_rates)
# always win over this table.
_DEFAULT_MODEL_RATES: dict[str, ModelRate] = {
    # --- logical tiers (fallback rung) ---
    "oss": ModelRate(input_per_mtok=0.10, output_per_mtok=0.40),
    "mid": ModelRate(input_per_mtok=0.80, output_per_mtok=4.00),
    "frontier": ModelRate(input_per_mtok=3.00, output_per_mtok=15.00),
    "router": ModelRate(input_per_mtok=0.10, output_per_mtok=0.40),
    # --- concrete model ids (entitlements.TIER_MODELS) ---
    # oss tier (open-weight, on-demand FMs)
    "openai.gpt-oss-20b-1:0": ModelRate(input_per_mtok=0.07, output_per_mtok=0.30),
    "openai.gpt-oss-120b-1:0": ModelRate(input_per_mtok=0.15, output_per_mtok=0.60),
    "google.gemma-3-12b-it": ModelRate(input_per_mtok=0.10, output_per_mtok=0.40),
    "google.gemma-3-4b-it": ModelRate(input_per_mtok=0.05, output_per_mtok=0.20),
    # mid tier (Claude Haiku, inference profiles)
    "us.anthropic.claude-3-5-haiku-20241022-v1:0": ModelRate(
        input_per_mtok=0.80, output_per_mtok=4.00
    ),
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": ModelRate(
        input_per_mtok=1.00, output_per_mtok=5.00
    ),
    # frontier tier (Claude Sonnet/Opus, inference profiles)
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": ModelRate(
        input_per_mtok=3.00, output_per_mtok=15.00
    ),
    "us.anthropic.claude-opus-4-1-20250805-v1:0": ModelRate(
        input_per_mtok=15.00, output_per_mtok=75.00
    ),
    # Embedding models (output rate unused).
    "embed-text": ModelRate(input_per_mtok=0.02),
    "embed-multimodal": ModelRate(input_per_mtok=0.06),
    "amazon.titan-embed-text-v2:0": ModelRate(input_per_mtok=0.02),
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

    def llm_rate(self, model_id: str, fallback_tier: str | None = None) -> ModelRate:
        """Rate for an LLM/embedding id. Resolution order:
          1. config override (PriceBook.model_rates) for the concrete id,
          2. the hard-default per-model rate for the concrete id,
          3. the hard-default rate for `fallback_tier` (the caller's known tier),
          4. the cheapest (oss) default — so an unknown id never blocks a call.

        `fallback_tier` is what fixes the #88 bug: the meter/chokepoint pass a concrete
        Bedrock id; if it isn't individually priced, an unlisted id resolves to its
        TIER's rate (a new frontier model prices at frontier, not oss). Config still wins.
        """
        if model_id in self.model_rates:
            return self.model_rates[model_id]
        if model_id in _DEFAULT_MODEL_RATES:
            return _DEFAULT_MODEL_RATES[model_id]
        if fallback_tier and fallback_tier in _DEFAULT_MODEL_RATES:
            return _DEFAULT_MODEL_RATES[fallback_tier]
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
