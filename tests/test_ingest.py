"""Unit tests for the ingest Lambda — no live AWS (boto3 clients stubbed).

Proves the FERPA-critical behaviour: a chunk is written ONLY to the index named
for its key's tenant prefix, and a key without a tenant prefix is skipped, never
guessed into some default index.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest import handler as ingest  # noqa: E402


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


class _FakeS3:
    def __init__(self, contents: dict[str, bytes]):
        self.contents = contents

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3 kwarg casing
        data = self.contents[Key]
        return {"Body": _FakeBody(data), "ContentLength": len(data)}


class _FakeBedrock:
    def __init__(self):
        self.calls = 0

    def invoke_model(self, modelId, body):  # noqa: N803
        self.calls += 1
        # bedrock-runtime invoke_model returns the payload under lowercase "body".
        return {"body": _FakeBody(b'{"embedding": [0.1, 0.2, 0.3]}')}


class _FakeVectors:
    def __init__(self):
        self.put_calls = []

    def put_vectors(self, vectorBucketName, indexName, vectors):  # noqa: N803
        self.put_calls.append({"index": indexName, "vectors": vectors})


@pytest.fixture
def stubs(monkeypatch):
    s3 = _FakeS3({"chem/syllabus.txt": b"hello world from chem"})
    bedrock = _FakeBedrock()
    vectors = _FakeVectors()
    monkeypatch.setattr(ingest, "_s3", s3)
    monkeypatch.setattr(ingest, "_bedrock", bedrock)
    monkeypatch.setattr(ingest, "_vectors", vectors)
    monkeypatch.setattr(ingest, "VECTOR_BUCKET", "agg-vectors-test")
    return s3, bedrock, vectors


def test_ingest_writes_to_tenant_index_only(stubs):
    _, bedrock, vectors = stubs
    n = ingest.ingest_object("agg-docs-test", "chem/syllabus.txt")
    assert n == 1
    assert len(vectors.put_calls) == 1
    call = vectors.put_calls[0]
    assert call["index"] == "agg-chem"  # tenant-derived, not defaulted
    v = call["vectors"][0]
    assert v["data"]["float32"] == [0.1, 0.2, 0.3]
    assert v["metadata"]["tenant"] == "chem"
    assert v["metadata"]["source_key"] == "chem/syllabus.txt"
    assert bedrock.calls == 1


def test_handler_skips_object_without_tenant_prefix(stubs):
    _, _, vectors = stubs
    event = {
        "Records": [{"s3": {"bucket": {"name": "agg-docs-test"}, "object": {"key": "bare.txt"}}}]
    }
    out = ingest.handler(event, None)
    assert out["processed"][0]["status"] == "skipped"
    assert vectors.put_calls == []  # nothing written for an untenanted object


def test_handler_url_decodes_key(stubs):
    s3, _, vectors = stubs
    s3.contents["chem/week 1.txt"] = b"spaces in name"
    event = {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": "agg-docs-test"},
                    "object": {"key": "chem/week+1.txt"},
                }
            }
        ]
    }
    out = ingest.handler(event, None)
    assert out["processed"][0]["status"] == "ok"
    assert vectors.put_calls[0]["index"] == "agg-chem"


def test_handler_isolates_per_object_failures(stubs):
    # One bad object must not abort the others in the batch.
    event = {
        "Records": [
            {"s3": {"bucket": {"name": "b"}, "object": {"key": "bare.txt"}}},
            {"s3": {"bucket": {"name": "b"}, "object": {"key": "chem/syllabus.txt"}}},
        ]
    }
    out = ingest.handler(event, None)
    statuses = [r["status"] for r in out["processed"]]
    assert statuses == ["skipped", "ok"]
