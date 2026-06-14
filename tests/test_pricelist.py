"""Unit tests for the deploy-time Price List parser (#90). No AWS — runs against a
recorded fixture (`tests/fixtures/pricelist_bedrock_us_east_1.json`) captured live
from us-east-1, trimmed to the 8 models in entitlements.TIER_MODELS."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cost.pricelist import (
    BEDROCK_ALIASES,
    ModelAlias,
    PriceListError,
    parse_price_list,
    rates_to_json,
)
from cost.pricing import ModelRate, load_baked_rates

_FIXTURE = Path(__file__).parent / "fixtures" / "pricelist_bedrock_us_east_1.json"


@pytest.fixture(scope="module")
def raw() -> dict[str, list[str]]:
    return json.loads(_FIXTURE.read_text())


def test_fixture_covers_both_offers(raw):
    assert "AmazonBedrockFoundationModels" in raw and raw["AmazonBedrockFoundationModels"]
    assert "AmazonBedrock" in raw and raw["AmazonBedrock"]


def test_parses_every_aliased_model(raw):
    rates = parse_price_list(raw)
    # Every model in the curated alias map resolves to a rate from the live data.
    assert set(rates) == set(BEDROCK_ALIASES)
    for r in rates.values():
        assert r.input_per_mtok > 0 and r.output_per_mtok > 0


def test_frontier_opus_matches_published_list_price(raw):
    # Opus 4.1: $15 in / $75 out per Mtok (verified live, us-east-1, 2026-06).
    rates = parse_price_list(raw)
    opus = rates["us.anthropic.claude-opus-4-1-20250805-v1:0"]
    assert opus.input_per_mtok == pytest.approx(15.0)
    assert opus.output_per_mtok == pytest.approx(75.0)


def test_sonnet_prefers_global_cross_region_rate(raw):
    # Sonnet 4.5 base in-region is 3.30/16.50; the _Global (cross-region inference
    # profile) list price is 3.00/15.00. We invoke via `us.` profiles, so prefer Global.
    rates = parse_price_list(raw)
    sonnet = rates["us.anthropic.claude-sonnet-4-5-20250929-v1:0"]
    assert sonnet.input_per_mtok == pytest.approx(3.0)
    assert sonnet.output_per_mtok == pytest.approx(15.0)


def test_open_weight_gpt_oss_normalised_from_per_1k(raw):
    # gpt-oss-20b: $0.07 in / $0.30 out per Mtok, quoted as $0.00007/$0.0003 per 1K.
    rates = parse_price_list(raw)
    g = rates["openai.gpt-oss-20b-1:0"]
    assert g.input_per_mtok == pytest.approx(0.07)
    assert g.output_per_mtok == pytest.approx(0.30)


def test_tiers_are_monotonic_from_live_data(raw):
    # The whole point of #88/#90: oss < mid < frontier on real numbers.
    rates = parse_price_list(raw)
    oss = rates["openai.gpt-oss-20b-1:0"].input_per_mtok
    mid = rates["us.anthropic.claude-haiku-4-5-20251001-v1:0"].input_per_mtok
    frontier = rates["us.anthropic.claude-opus-4-1-20250805-v1:0"].input_per_mtok
    assert oss < mid < frontier


def test_unmatched_alias_raises_not_silently_mispriced(raw):
    # A typo'd servicename must FAIL LOUD — never silently fall through to a wrong
    # (or oss) rate for real money.
    aliases = {"x.model": ModelAlias(servicename="No Such Model (Amazon Bedrock Edition)")}
    with pytest.raises(PriceListError):
        parse_price_list(raw, aliases)


def test_empty_alias_map_yields_empty_rates(raw):
    assert parse_price_list(raw, {}) == {}


def test_parsed_rates_are_bakeable_into_pricebook(raw):
    # The result drops straight into PriceBook(model_rates=...) and resolves by id.
    from cost.pricing import PriceBook

    pb = PriceBook(model_rates=parse_price_list(raw))
    opus = pb.llm_rate("us.anthropic.claude-opus-4-1-20250805-v1:0")
    assert opus.input_per_mtok == pytest.approx(15.0)


# --- baked-rates artifact round-trip (deploy-time write -> runtime load) ------


def test_rates_json_round_trips_through_load_baked_rates(raw, tmp_path):
    rates = parse_price_list(raw)
    artifact = tmp_path / "model_rates.json"
    artifact.write_text(rates_to_json(rates))
    loaded = load_baked_rates(str(artifact))
    assert set(loaded) == set(rates)
    opus = loaded["us.anthropic.claude-opus-4-1-20250805-v1:0"]
    assert opus.input_per_mtok == pytest.approx(15.0)
    assert opus.output_per_mtok == pytest.approx(75.0)


def test_load_baked_rates_missing_path_is_empty():
    assert load_baked_rates("") == {}
    assert load_baked_rates("/no/such/model_rates.json") == {}


def test_default_pricebook_uses_baked_rates_when_env_set(raw, tmp_path, monkeypatch):
    artifact = tmp_path / "model_rates.json"
    artifact.write_text(rates_to_json(parse_price_list(raw)))
    monkeypatch.setenv("AGATE_MODEL_RATES_PATH", str(artifact))
    from cost.pricing import default_pricebook

    pb = default_pricebook()
    # Baked authoritative rate beats the hard-default fallback for the same id.
    opus = pb.llm_rate("us.anthropic.claude-opus-4-1-20250805-v1:0")
    assert opus.input_per_mtok == pytest.approx(15.0)


def test_default_pricebook_falls_back_to_hard_defaults_without_baked_file(tmp_path, monkeypatch):
    # Point at a definitely-missing path so neither the env override nor the
    # conventional cost/model_rates.json is picked up.
    monkeypatch.setenv("AGATE_MODEL_RATES_PATH", str(tmp_path / "absent.json"))
    from cost.pricing import default_pricebook

    pb = default_pricebook()
    assert pb.model_rates == {}  # no baked overrides
    # Still prices a concrete id via the hard-default table (the #88 path).
    assert pb.llm_rate("us.anthropic.claude-opus-4-1-20250805-v1:0").input_per_mtok > 0


def test_model_rate_constructible_for_loader():
    # The loader builds ModelRate from the JSON shape it writes.
    r = ModelRate(input_per_mtok=1.0, output_per_mtok=2.0)
    assert (r.input_per_mtok, r.output_per_mtok) == (1.0, 2.0)
