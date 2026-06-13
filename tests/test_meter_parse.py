"""Unit tests for the pure invocation-log -> spend parsing (§7.2, §13.6). No AWS."""

from __future__ import annotations

import pytest
from cost.pricing import ModelRate, PriceBook
from meter import parse_invocation_record, spend_key, spend_rollup_key
from meter.parse import RecordError


# A representative Bedrock model-invocation log record.
def record(**over):
    rec = {
        "timestamp": "2026-06-12T18:00:00Z",
        "modelId": "oss",
        "identity": {"arn": "arn:aws:sts::123:assumed-role/agg-authenticated/student-7"},
        "input": {"inputTokenCount": 1000},
        "output": {"outputTokenCount": 500},
        "requestMetadata": {"agg:tenant": "chem"},
    }
    rec.update(over)
    return rec


def test_parses_identity_tenant_period_and_cost():
    pb = PriceBook(model_rates={"oss": ModelRate(input_per_mtok=0.10, output_per_mtok=0.40)})
    s = parse_invocation_record(record(), pricebook=pb)
    assert s.user == "student-7"
    assert s.tenant == "chem"
    assert s.period == "2026-06"
    assert s.input_tokens == 1000 and s.output_tokens == 500
    # 1000/1e6*0.10 + 500/1e6*0.40 = 0.0003
    assert s.cost_usd == pytest.approx(0.0003)


def test_tenant_explicit_overrides_metadata():
    s = parse_invocation_record(record(), tenant="kempner")
    assert s.tenant == "kempner"


def test_tenant_falls_back_to_unknown():
    rec = record()
    del rec["requestMetadata"]
    assert parse_invocation_record(rec).tenant == "unknown"


def test_user_unknown_when_arn_not_assumed_role():
    s = parse_invocation_record(record(identity={"arn": "arn:aws:iam::123:user/admin"}))
    assert s.user == "unknown"


def test_missing_model_id_raises():
    rec = record()
    del rec["modelId"]
    with pytest.raises(RecordError):
        parse_invocation_record(rec)


def test_missing_token_counts_raises():
    with pytest.raises(RecordError):
        parse_invocation_record(record(input={}, output={}))


def test_one_sided_token_count_is_ok():
    # Some records only carry one side; the other defaults to 0.
    s = parse_invocation_record(record(output={}))
    assert s.input_tokens == 1000 and s.output_tokens == 0


def test_unparseable_timestamp_raises():
    with pytest.raises(RecordError):
        parse_invocation_record(record(timestamp="not-a-date"))


def test_period_rolls_over_by_month():
    assert parse_invocation_record(record(timestamp="2027-01-15T00:00:00Z")).period == "2027-01"


def test_spend_keys():
    assert spend_key("chem", "student-7", "2026-06") == "chem#student-7#2026-06"
    assert spend_rollup_key("chem", "2026-06") == "chem#2026-06"


def test_unknown_model_id_still_prices_via_pricebook_default():
    # A concrete model id not in the pricebook resolves to the default rate, never raises.
    s = parse_invocation_record(record(modelId="anthropic.claude-opus-4-1-20250805-v1:0"))
    assert s.cost_usd >= 0
