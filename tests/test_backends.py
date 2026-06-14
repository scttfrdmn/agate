"""Unit tests for the agent Bedrock backend's spend-attribution metadata (#77).

No AWS — a fake bedrock-runtime client captures the converse kwargs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import backends  # noqa: E402


class _FakeRt:
    def __init__(self):
        self.last_kwargs = None

    def converse(self, **kwargs):
        self.last_kwargs = kwargs
        return {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 3, "outputTokens": 5},
        }


def _backend(monkeypatch, metadata):
    import boto3

    fake = _FakeRt()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)
    return backends.BedrockBackend("us-east-1", request_metadata=metadata), fake


def test_meta_value_sanitises_and_truncates():
    assert backends._meta_value("arts-sci/chemistry") == "arts-sci/chemistry"  # allowed chars
    assert backends._meta_value("bad!value*pct%") == "bad-value-pct-"  # !/*/% -> -
    assert len(backends._meta_value("x" * 500)) == 256


def test_converse_attaches_request_metadata(monkeypatch):
    b, fake = _backend(monkeypatch, {"agate:tenant": "demo", "agate:user": "u1"})
    b.converse("model-x", "sys", "hi", 64)
    assert fake.last_kwargs["requestMetadata"] == {"agate:tenant": "demo", "agate:user": "u1"}


def test_converse_omits_metadata_when_none(monkeypatch):
    b, fake = _backend(monkeypatch, None)
    b.converse("model-x", "", "hi", 64)
    assert "requestMetadata" not in fake.last_kwargs


def test_empty_metadata_values_dropped(monkeypatch):
    b, fake = _backend(monkeypatch, {"agate:tenant": "demo", "agate:user": ""})
    b.converse("model-x", "", "hi", 64)
    assert fake.last_kwargs["requestMetadata"] == {"agate:tenant": "demo"}
