"""Tests for the spend Lambda — no AWS (S3 + DynamoDB stubbed)."""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meter import handler as meter  # noqa: E402


class _Body:
    def __init__(self, data: bytes):
        self._d = data

    def read(self):
        return self._d


class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": _Body(self.objects[Key])}


class _FakeTable:
    def __init__(self):
        self.rows: dict[str, float] = {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):  # noqa: N803
        pk = Key["pk"]
        self.rows[pk] = self.rows.get(pk, 0.0) + float(ExpressionAttributeValues[":a"])

    def get_item(self, Key):  # noqa: N803
        pk = Key["pk"]
        return {"Item": {"pk": pk, "spend_usd": self.rows[pk]}} if pk in self.rows else {}


class _FakeDdb:
    def __init__(self, table):
        self._t = table

    def Table(self, _name):  # noqa: N802
        return self._t


def _rec(user, tenant, tokens_in, tokens_out, ts="2026-06-12T18:00:00Z", model="oss"):
    return {
        "timestamp": ts,
        "modelId": model,
        "identity": {"arn": f"arn:aws:sts::123:assumed-role/agg-authenticated/{user}"},
        "input": {"inputTokenCount": tokens_in},
        "output": {"outputTokenCount": tokens_out},
        "requestMetadata": {"agg:tenant": tenant},
    }


@pytest.fixture
def wired(monkeypatch):
    table = _FakeTable()
    monkeypatch.setattr(meter, "_ddb", _FakeDdb(table))
    monkeypatch.setattr(meter, "SPEND_TABLE", "agg-spend")
    return table


def _event(key):
    return {"Records": [{"s3": {"bucket": {"name": "logs"}, "object": {"key": key}}}]}


def test_meters_records_into_user_and_rollup_rows(wired, monkeypatch):
    lines = "\n".join(
        json.dumps(r)
        for r in [_rec("student-7", "chem", 1_000_000, 0), _rec("student-7", "chem", 1_000_000, 0)]
    ).encode()
    monkeypatch.setattr(meter, "_s3", _FakeS3({"logs/a.json": lines}))

    out = meter.handler(_event("logs/a.json"), None)
    assert out == {"metered": 2, "skipped": 0}
    # default oss rate input_per_mtok=0.10 -> each record $0.10, two records $0.20
    assert meter.read_spend("chem", "student-7", "2026-06") == pytest.approx(0.20)
    # tenant rollup also accumulated
    assert wired.rows["chem#2026-06"] == pytest.approx(0.20)


def test_gzip_log_object_is_read(wired, monkeypatch):
    data = gzip.compress(json.dumps(_rec("u1", "chem", 1_000_000, 0)).encode())
    monkeypatch.setattr(meter, "_s3", _FakeS3({"logs/a.json.gz": data}))
    out = meter.handler(_event("logs/a.json.gz"), None)
    assert out["metered"] == 1


def test_bad_record_is_skipped_not_fatal(wired, monkeypatch):
    good = json.dumps(_rec("u1", "chem", 1_000_000, 0))
    bad = json.dumps({"modelId": "oss"})  # no tokens/timestamp -> RecordError
    monkeypatch.setattr(meter, "_s3", _FakeS3({"logs/a.json": (good + "\n" + bad).encode()}))
    out = meter.handler(_event("logs/a.json"), None)
    assert out == {"metered": 1, "skipped": 1}


def test_read_spend_zero_when_absent(wired):
    assert meter.read_spend("chem", "nobody", "2026-06") == 0.0


def test_soft_cap_reads_authoritative_spend(wired, monkeypatch):
    # End-to-end with the soft cap: meter spend, then the broker's decision.
    from cost import evaluate_soft_cap

    monkeypatch.setattr(
        meter,
        "_s3",
        _FakeS3({"logs/a.json": json.dumps(_rec("u1", "chem", 5_000_000, 0)).encode()}),
    )
    meter.handler(_event("logs/a.json"), None)
    spend = meter.read_spend("chem", "u1", "2026-06")  # 5M * 0.10/M = $0.50
    assert spend == pytest.approx(0.50)
    assert evaluate_soft_cap(spend, budget=0.40).decision == "deny"  # over budget
    assert evaluate_soft_cap(spend, budget=1.00).decision == "allow"  # under budget
