"""Unit tests for the pure cost engine (§7.2, §13.6). No AWS."""

from __future__ import annotations

import pytest
from cost import CostMeter, PriceBook, default_pricebook
from cost.pricing import ModelRate

# --- pricing resolution -----------------------------------------------------


def test_config_override_wins_over_default():
    pb = PriceBook(model_rates={"frontier": ModelRate(input_per_mtok=1.0, output_per_mtok=2.0)})
    rate = pb.llm_rate("frontier")
    assert rate.input_per_mtok == 1.0
    assert rate.output_per_mtok == 2.0


def test_hard_default_when_no_override():
    rate = default_pricebook().llm_rate("frontier")
    assert rate.input_per_mtok > 0  # a default exists


def test_unknown_model_falls_back_never_crashes():
    rate = default_pricebook().llm_rate("some-unlisted-model")
    assert rate.input_per_mtok > 0  # falls back to cheapest, no exception


# --- #88: concrete model ids price by id, not the oss fallback --------------


def test_concrete_frontier_id_prices_far_above_oss():
    # The bug: a frontier Opus id used to resolve to the oss rate. It must now price
    # at the frontier rate — materially higher than an oss model.
    pb = default_pricebook()
    opus = pb.llm_rate("us.anthropic.claude-opus-4-1-20250805-v1:0")
    gpt_oss = pb.llm_rate("openai.gpt-oss-20b-1:0")
    assert opus.input_per_mtok > gpt_oss.input_per_mtok * 10
    assert opus.output_per_mtok > gpt_oss.output_per_mtok * 10


def test_each_tier_id_resolves_distinctly():
    pb = default_pricebook()
    oss = pb.llm_rate("openai.gpt-oss-20b-1:0").input_per_mtok
    mid = pb.llm_rate("us.anthropic.claude-haiku-4-5-20251001-v1:0").input_per_mtok
    frontier = pb.llm_rate("us.anthropic.claude-opus-4-1-20250805-v1:0").input_per_mtok
    assert oss < mid < frontier  # distinct, monotonic by tier


def test_fallback_tier_prices_unlisted_id_at_its_tier_not_oss():
    pb = default_pricebook()
    # An unlisted id WITH a frontier fallback prices at frontier, not oss.
    r = pb.llm_rate("anthropic.future-frontier-model", fallback_tier="frontier")
    assert r.input_per_mtok == pb.llm_rate("frontier").input_per_mtok
    # Without a fallback it still degrades to oss (never crashes).
    r2 = pb.llm_rate("anthropic.future-frontier-model")
    assert r2.input_per_mtok == pb.llm_rate("oss").input_per_mtok


def test_config_override_still_wins_over_per_model_default():
    pb = PriceBook(
        model_rates={
            "us.anthropic.claude-opus-4-1-20250805-v1:0": ModelRate(
                input_per_mtok=1.0, output_per_mtok=2.0
            )
        }
    )
    r = pb.llm_rate("us.anthropic.claude-opus-4-1-20250805-v1:0", fallback_tier="frontier")
    assert r.input_per_mtok == 1.0  # config beats the baked default


def test_s3_vectors_retrieval_uses_config_fallback():
    # S3 Vectors isn't in the Price List → config/hard-default only.
    assert default_pricebook().retrieval_rate_per_k() > 0
    pb = PriceBook(retrieval_per_k=0.99)
    assert pb.retrieval_rate_per_k() == 0.99


# --- CostMeter dollar math --------------------------------------------------


def test_add_llm_computes_dollars_from_tokens():
    pb = PriceBook(model_rates={"frontier": ModelRate(input_per_mtok=3.0, output_per_mtok=15.0)})
    meter = CostMeter(pb)
    # 1M input @ $3 + 1M output @ $15 = $18
    cost = meter.add_llm(
        "panel · frontier",
        "frontier",
        "frontier",
        {"inputTokens": 1_000_000, "outputTokens": 1_000_000},
    )
    assert cost == pytest.approx(18.0)
    assert meter.total == pytest.approx(18.0)


def test_add_llm_partial_tokens():
    pb = PriceBook(model_rates={"oss": ModelRate(input_per_mtok=0.10, output_per_mtok=0.40)})
    meter = CostMeter(pb)
    cost = meter.add_llm("ask", "oss", "oss", {"inputTokens": 1000, "outputTokens": 500})
    # 1000/1e6*0.10 + 500/1e6*0.40 = 0.0001 + 0.0002 = 0.0003
    assert cost == pytest.approx(0.0003)


def test_add_embedding_input_only():
    pb = PriceBook(model_rates={"embed-text": ModelRate(input_per_mtok=0.02)})
    meter = CostMeter(pb)
    cost = meter.add_embedding("ingest", "embed-text", 500_000)
    assert cost == pytest.approx(0.01)


def test_add_retrieval_per_thousand():
    meter = CostMeter(PriceBook(retrieval_per_k=0.25))
    assert meter.add_retrieval("rag query", queries=1) == pytest.approx(0.00025)
    assert meter.add_retrieval("rag batch", queries=1000) == pytest.approx(0.25)


def test_add_compute_per_second():
    meter = CostMeter(PriceBook(compute_per_sec=0.0002))
    assert meter.add_compute("analyze · exec", 10) == pytest.approx(0.002)


# --- receipt ----------------------------------------------------------------


def test_receipt_itemises_all_kinds_and_totals():
    meter = CostMeter(default_pricebook())
    meter.add_llm(
        "panel · frontier", "frontier", "frontier", {"inputTokens": 1000, "outputTokens": 200}
    )
    meter.add_compute("analyze · exec", 2.0)
    meter.add_retrieval("rag", 1)
    receipt = meter.receipt()
    kinds = {r.kind for r in receipt.rows}
    assert kinds == {"llm", "compute", "retrieval"}
    assert receipt.total == pytest.approx(sum(r.cost for r in receipt.rows))


def test_receipt_to_event_shape():
    meter = CostMeter(default_pricebook())
    meter.add_llm("ask", "oss", "oss", {"inputTokens": 100, "outputTokens": 50})
    ev = meter.receipt().to_event()
    assert ev["type"] == "receipt"
    assert ev["rows"][0]["kind"] == "llm"
    assert "total" in ev


def test_rows_carry_provenance():
    meter = CostMeter(default_pricebook())
    meter.add_llm("panel · frontier", "frontier", "model-x", {"inputTokens": 10, "outputTokens": 5})
    row = meter.rows[0]
    assert row.model_id == "model-x"
    assert row.input_tokens == 10
    assert row.output_tokens == 5


def test_meter_is_threadsafe_under_parallel_adds():
    import threading

    meter = CostMeter(PriceBook(model_rates={"oss": ModelRate(input_per_mtok=1.0)}))

    def worker():
        for _ in range(100):
            meter.add_llm("x", "oss", "oss", {"inputTokens": 1_000_000, "outputTokens": 0})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 8 threads × 100 calls × $1 each = $800; no lost updates.
    assert meter.total == pytest.approx(800.0)
    assert len(meter.rows) == 800


def test_meter_satisfies_orchestration_protocol():
    # add_llm/add_compute/total are what run_panel/run_analyze call.
    meter = CostMeter(default_pricebook())
    assert hasattr(meter, "add_llm") and hasattr(meter, "add_compute")
    assert isinstance(meter.total, float)
