"""Deploy-time AWS Price List → baked per-model rates (#90, follows #88).

#88 fixed the key-mismatch bug and seeded `pricing._DEFAULT_MODEL_RATES` with
best-effort hand-entered list prices. This module replaces those with
AUTHORITATIVE rates pulled from the AWS Price List API — but **at deploy time**,
never on the request hot path (NO CLOCKS). The flow:

    raw PriceList JSON strings  ──parse_price_list()──▶  {model_id: ModelRate}
              ▲                                                    │
        fetch_bedrock_price_list()                          bake into config /
        (the only boto3 surface)                            PriceBook.model_rates

The parser is PURE (no boto3, no network) so it unit-tests against a recorded
fixture. Only `fetch_bedrock_price_list` touches AWS, and only `pricing:GetProducts`
(read-only). `cost/` stays import-clean — no `agate` import; the caller supplies
the alias map keyed by the concrete ids it cares about.

## Why this is fiddly (verified live against us-east-1, 2026-06)

The Price List does NOT key rows by the concrete Bedrock model id we invoke. Two
offer codes, two shapes:

  * `AmazonBedrockFoundationModels` — Anthropic Claude etc. Identified ONLY by a
    human `servicename` ("Claude Opus 4.1 (Amazon Bedrock Edition)"). Each model
    fans out into many `usagetype`s: InputTokenCount / OutputTokenCount plus
    Cache*, Batch, Reserved, *_Global variants. We want the plain per-token
    input+output. For cross-region inference profiles (the `us.` ids we actually
    invoke) the `_Global` variant is the correct list price — the base in-region
    variant carries a ~10% premium. So we PREFER `_Global` when present.

  * `AmazonBedrock` — open-weight gpt-oss / Gemma. Identified by a `model`
    attribute ("gpt-oss-20b", "Gemma 3 12B") and an `inferenceType` ("Input
    tokens" / "Output tokens", plus flex/priority/batch tiers we ignore).

Units are **per 1K tokens**; we normalise to per-MILLION (×1000) to match
`ModelRate`. Because the live catalog moves ahead of our pinned ids (it may show
"Claude Opus 4.5" while we still pin opus-4-1), the alias map may point a pinned
id at the NEAREST live model — that substitution is explicit in the map, not
guessed here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from cost.pricing import ModelRate

# The two Bedrock Price List offer codes (us-east-1 endpoint only).
OFFER_FOUNDATION_MODELS = "AmazonBedrockFoundationModels"
OFFER_BEDROCK = "AmazonBedrock"


@dataclass(frozen=True, slots=True)
class ModelAlias:
    """How to find ONE concrete model id in the Price List.

    Exactly one of `servicename` / `model_attr` identifies the offer + row group:
      * servicename → AmazonBedrockFoundationModels (Claude et al.), matched exactly
        against the product `servicename` attribute.
      * model_attr  → AmazonBedrock (gpt-oss/Gemma), matched exactly against the
        product `model` attribute.

    `note` records a nearest-model substitution when the pinned id isn't in the
    live catalog (e.g. opus-4-1 priced off "Claude Opus 4.5").
    """

    servicename: str | None = None
    model_attr: str | None = None
    note: str = ""


# Curated alias map: concrete Bedrock model id (entitlements.TIER_MODELS) -> how to
# locate its rate in the Price List. Hand-maintained ON PURPOSE — the Price List has
# no concrete ids, and a fuzzy auto-match could silently mis-price real money. When
# AWS adds/renames a model, update one line here. servicenames/model attrs verified
# live in us-east-1 (2026-06). All four Claude ids and all four open-weight ids are
# present today, so no nearest-model substitution is currently needed.
BEDROCK_ALIASES: dict[str, ModelAlias] = {
    # oss tier — AmazonBedrock offer, `model` attribute.
    "openai.gpt-oss-20b-1:0": ModelAlias(model_attr="gpt-oss-20b"),
    "openai.gpt-oss-120b-1:0": ModelAlias(model_attr="gpt-oss-120b"),
    "google.gemma-3-12b-it": ModelAlias(model_attr="Gemma 3 12B"),
    "google.gemma-3-4b-it": ModelAlias(model_attr="Gemma 3 4B"),
    # mid tier — AmazonBedrockFoundationModels offer, `servicename`.
    "us.anthropic.claude-3-5-haiku-20241022-v1:0": ModelAlias(
        servicename="Claude 3.5 Haiku (Amazon Bedrock Edition)"
    ),
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": ModelAlias(
        servicename="Claude Haiku 4.5 (Amazon Bedrock Edition)"
    ),
    # frontier tier — AmazonBedrockFoundationModels offer, `servicename`.
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": ModelAlias(
        servicename="Claude Sonnet 4.5 (Amazon Bedrock Edition)"
    ),
    "us.anthropic.claude-opus-4-1-20250805-v1:0": ModelAlias(
        servicename="Claude Opus 4.1 (Amazon Bedrock Edition)"
    ),
}

_PER_1K_TO_PER_MTOK = 1000.0


class PriceListError(Exception):
    """A Price List row could not be parsed into a clean per-token rate."""


def _on_demand_price(product: dict) -> tuple[float, str] | None:
    """The single OnDemand (USD, unit) for a product, or None if unpriced.

    FoundationModels reports unit "Units" (already per-MILLION); AmazonBedrock
    reports "1K tokens". The caller normalises both via `_norm_to_mtok`."""
    for term in product.get("terms", {}).get("OnDemand", {}).values():
        for pd in term.get("priceDimensions", {}).values():
            usd = pd.get("pricePerUnit", {}).get("USD")
            if usd is None:
                continue
            return float(usd), pd.get("unit", "")
    return None


def _norm_to_mtok(price: float, unit: str) -> float:
    """Normalise a Price List price to USD per MILLION tokens.

    `1K tokens` → ×1000. `Units` (FoundationModels per-token) is ALSO per-1K in
    practice — its raw value is the per-1K price (e.g. Opus input = 15.0), so it is
    already per-MILLION when read directly. We detect by magnitude-free rule: the
    `1K tokens` unit is multiplied; `Units` is taken as-is (already /Mtok)."""
    if unit == "1K tokens":
        return price * _PER_1K_TO_PER_MTOK
    return price  # "Units" rows are already quoted per-million (e.g. 15.0 = $15/Mtok)


def _parse_foundation_model(rows: list[dict], servicename: str) -> ModelRate | None:
    """Extract (input, output) per-Mtok for a FoundationModels servicename.

    Prefer the `_Global` usagetype variant (cross-region inference-profile list
    price — the `us.` ids we invoke); fall back to the base in-region variant. Skip
    Cache*, Batch, Reserved, ProvisionedThroughput, CustomModel, LatencyOptimized,
    TPM rows entirely."""
    inp: dict[str, float] = {}
    out: dict[str, float] = {}
    for product in rows:
        a = product["product"]["attributes"]
        if a.get("servicename") != servicename:
            continue
        ut = a.get("usagetype", "")
        low = ut.lower()
        if any(
            bad in low
            for bad in (
                "cache",
                "batch",
                "reserved",
                "provisionedthroughput",
                "custommodel",
                "latencyoptimized",
                "tpm",
            )
        ):
            continue
        priced = _on_demand_price(product)
        if priced is None:
            continue
        price, unit = priced
        mtok = _norm_to_mtok(price, unit)
        variant = "global" if "_global" in low else "regional"
        if "inputtokencount" in low:
            inp[variant] = mtok
        elif "outputtokencount" in low:
            out[variant] = mtok
    in_rate = inp.get("global", inp.get("regional"))
    out_rate = out.get("global", out.get("regional"))
    if in_rate is None or out_rate is None:
        return None
    return ModelRate(input_per_mtok=in_rate, output_per_mtok=out_rate)


def _parse_bedrock_open_weight(rows: list[dict], model_attr: str) -> ModelRate | None:
    """Extract (input, output) per-Mtok for an AmazonBedrock `model` (gpt-oss/Gemma).

    Use the STANDARD on-demand tier only: inferenceType "Input tokens"/"Output
    tokens" (skip flex/priority/batch). These rows quote `1K tokens`, normalised ×1000."""
    in_rate: float | None = None
    out_rate: float | None = None
    for product in rows:
        a = product["product"]["attributes"]
        if a.get("model") != model_attr:
            continue
        it = a.get("inferenceType", "")
        ut = a.get("usagetype", "").lower()
        # Standard tier only: exact "Input tokens"/"Output tokens" (not flex/priority),
        # and skip batch/flex/priority usagetypes and the duplicate mantle rows.
        if any(x in ut for x in ("batch", "flex", "priority", "mantle")):
            continue
        priced = _on_demand_price(product)
        if priced is None:
            continue
        price, unit = priced
        mtok = _norm_to_mtok(price, unit)
        if it == "Input tokens":
            in_rate = mtok
        elif it == "Output tokens":
            out_rate = mtok
    if in_rate is None or out_rate is None:
        return None
    return ModelRate(input_per_mtok=in_rate, output_per_mtok=out_rate)


def parse_price_list(
    raw: dict[str, list[str]],
    aliases: dict[str, ModelAlias] | None = None,
) -> dict[str, ModelRate]:
    """Pure parse: raw Price List (offer code -> list of product JSON strings) +
    alias map -> {concrete model id: ModelRate} in USD/Mtok.

    `raw` mirrors the GetProducts response: a dict keyed by offer code, each value a
    list of the JSON-string products from `PriceList`. Unmatched ids are simply
    absent from the result (the caller keeps the #88 default for them) — never raises
    on a missing model, only on a model that matched but yielded no clean rate."""
    aliases = aliases if aliases is not None else BEDROCK_ALIASES
    parsed: dict[str, list[dict]] = {
        offer: [json.loads(s) for s in strings] for offer, strings in raw.items()
    }
    fm_rows = parsed.get(OFFER_FOUNDATION_MODELS, [])
    bd_rows = parsed.get(OFFER_BEDROCK, [])

    rates: dict[str, ModelRate] = {}
    for model_id, alias in aliases.items():
        if alias.servicename is not None:
            rate = _parse_foundation_model(fm_rows, alias.servicename)
            label = alias.servicename
        elif alias.model_attr is not None:
            rate = _parse_bedrock_open_weight(bd_rows, alias.model_attr)
            label = alias.model_attr
        else:  # pragma: no cover - guarded by ModelAlias construction
            raise PriceListError(f"alias for {model_id} names neither servicename nor model_attr")
        if rate is None:
            raise PriceListError(
                f"matched {label!r} for {model_id} but found no clean input+output rate"
            )
        rates[model_id] = rate
    return rates


def fetch_bedrock_price_list(region: str = "us-east-1") -> dict[str, list[str]]:
    """Live (read-only) fetch of the two Bedrock offers, filtered to `region`, in the
    shape `parse_price_list` expects. The ONLY boto3 surface here; us-east-1 endpoint
    only (`pricing:GetProducts`). Run at DEPLOY time, not per request."""
    import boto3  # local import keeps the parser importable without boto3

    client = boto3.client("pricing", region_name="us-east-1")
    raw: dict[str, list[str]] = {}
    for offer in (OFFER_FOUNDATION_MODELS, OFFER_BEDROCK):
        strings: list[str] = []
        paginator = client.get_paginator("get_products")
        for page in paginator.paginate(
            ServiceCode=offer,
            Filters=[{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}],
        ):
            strings.extend(page["PriceList"])
        raw[offer] = strings
    return raw


def build_pricebook_rates(region: str = "us-east-1") -> dict[str, ModelRate]:
    """Convenience: live-fetch + parse -> {model id: ModelRate} ready to bake into
    `PriceBook(model_rates=...)`. Deploy-time only."""
    return parse_price_list(fetch_bedrock_price_list(region))


def rates_to_json(rates: dict[str, ModelRate]) -> str:
    """Serialise {model id: ModelRate} to the baked-rates JSON the runtime loads
    (cost.pricing.load_baked_rates). Stable key order so the artifact diffs cleanly."""
    return json.dumps(
        {
            mid: {
                # round to 6dp — kills float-repr noise (0.09000000000000001) from the
                # per-1K→per-Mtok ×1000; rates are quoted to far fewer places than this.
                "input_per_mtok": round(r.input_per_mtok, 6),
                "output_per_mtok": round(r.output_per_mtok, 6),
            }
            for mid, r in sorted(rates.items())
        },
        indent=2,
    )


def _main(argv: list[str] | None = None) -> int:
    """Deploy-time CLI: fetch live Bedrock rates and write the baked-rates JSON.

        uv run python -m cost.pricelist --out infra/assets/model_rates.json

    Read-only against AWS (`pricing:GetProducts`, us-east-1). The written file is
    what the meter/chokepoint Lambdas load via AGATE_MODEL_RATES_PATH — no runtime
    Price List call (NO CLOCKS)."""
    import argparse

    parser = argparse.ArgumentParser(prog="cost.pricelist", description=__doc__)
    parser.add_argument("--region", default="us-east-1", help="Bedrock region to price")
    parser.add_argument("--out", help="write baked-rates JSON here (default: stdout)")
    args = parser.parse_args(argv)

    rates = build_pricebook_rates(args.region)
    payload = rates_to_json(rates)
    if args.out:
        from pathlib import Path

        Path(args.out).write_text(payload + "\n")
        print(f"wrote {len(rates)} model rates -> {args.out}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(_main())
