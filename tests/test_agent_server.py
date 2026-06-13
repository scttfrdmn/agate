"""Tests for the agent container's invocation handling. No AWS — backends stubbed.

Exercises run_invocation end-to-end (dispatch + terminal receipt) and the
events→blob encoding, without the Bedrock/Code-Interpreter clients.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import server  # noqa: E402
from agent.backends import decode_payload, encode_payload  # noqa: E402


class StubBackend:
    def __init__(self, *_a, **_k):
        pass

    def converse(self, tier, system, prompt, max_tokens):
        word = "SYNTHESIS" if "Reply with one word" in system else "The cited answer."
        return word, {"inputTokens": 10, "outputTokens": 3}, None


@pytest.fixture
def stub_backends(monkeypatch):
    monkeypatch.setattr(server, "BedrockBackend", StubBackend)
    monkeypatch.setattr(server, "CODE_INTERPRETER_ID", "")  # no runner -> Ask/Panel only


def test_run_invocation_emits_stream_then_receipt(stub_backends):
    events = server.run_invocation({"question": "what is x?", "evidence": "DOC1"})
    types = [e["type"] for e in events]
    assert "route" in types
    assert "answer" in types
    # always closes with a receipt
    assert types[-1] == "receipt"
    receipt = events[-1]
    assert "rows" in receipt and "total" in receipt
    assert any(r["kind"] == "llm" for r in receipt["rows"])


def test_run_invocation_bad_payload_surfaces_error_then_receipt(stub_backends):
    events = server.run_invocation({})  # no question
    assert events[0]["type"] == "answer"
    assert events[0]["title"] == "error"
    assert events[-1]["type"] == "receipt"


def test_unverifiable_token_falls_back_to_oss(stub_backends, monkeypatch):
    # SEC-4b: no/invalid token -> oss tier -> a frontier model in the payload is
    # rejected by dispatch before any model call.
    events = server.run_invocation(
        {
            "question": "compare",
            "mode": "DEBATE",
            "evidence": "e",
            "roster": [
                {
                    "tier": "us.anthropic.claude-opus-4-1-20250805-v1:0",
                    "label": "x",
                    "max_tokens": 64,
                }
            ],
            "adjudicator": {"tier": "openai.gpt-oss-20b-1:0", "label": "adj", "max_tokens": 64},
            "idp_token": "",  # unverifiable -> oss
        }
    )
    # dispatch raised InvocationError (model not entitled) -> surfaced as error answer
    assert any(e["type"] == "answer" and e.get("title") == "error" for e in events)
    assert any("not entitled" in e.get("text", "") for e in events if e["type"] == "answer")


def test_verified_frontier_tier_allows_frontier_model(stub_backends, monkeypatch):
    # A verified frontier tier permits a frontier model (the positive case).
    monkeypatch.setattr(server, "_verified_tier", lambda payload: "frontier")
    events = server.run_invocation(
        {
            "question": "compare",
            "mode": "DEBATE",
            "evidence": "e",
            "roster": [
                {
                    "tier": "us.anthropic.claude-opus-4-1-20250805-v1:0",
                    "label": "x",
                    "max_tokens": 64,
                }
            ],
            "adjudicator": {
                "tier": "us.anthropic.claude-opus-4-1-20250805-v1:0",
                "label": "adj",
                "max_tokens": 64,
            },
        }
    )
    # no entitlement error; ran to a receipt
    assert not any(e.get("title") == "error" for e in events if e["type"] == "answer")
    assert events[-1]["type"] == "receipt"


def test_events_to_blob_is_ndjson():
    blob = server._events_to_blob(
        [{"type": "answer", "text": "hi"}, {"type": "cost", "total": 1.0}]
    )
    lines = blob.decode().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "answer"
    assert json.loads(lines[1])["total"] == 1.0


def test_payload_codec_roundtrip():
    obj = {"question": "q", "mode": "DEBATE", "roster": [{"tier": "frontier"}]}
    assert decode_payload(encode_payload(obj)) == obj
    assert decode_payload("") == {}
