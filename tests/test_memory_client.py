"""Tests for the runtime→memory bridge (#130b). No AWS — the Lambda client is stubbed.

The bridge is best-effort and opt-in: disabled (no tool ARN) it is a silent no-op; enabled
it invokes the memory tool Lambda forwarding the verified token, and NEVER raises (a memory
failure must not break the turn).
"""

from __future__ import annotations

import json

import pytest
from agent import memory_client as mc


class _FakeLambda:
    """Captures invoke() calls; returns a scripted Lambda response envelope."""

    def __init__(self, body: dict, status: int = 200, raise_on_invoke: bool = False):
        self._body = body
        self._status = status
        self._raise = raise_on_invoke
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)
        if self._raise:
            raise RuntimeError("lambda boom")
        out = {"statusCode": self._status, "body": json.dumps(self._body)}

        class _P:
            def read(_self):
                return json.dumps(out).encode("utf-8")

        return {"Payload": _P()}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # default: a tool is wired, with a benign records response
    monkeypatch.setattr(mc, "MEMORY_TOOL_ARN", "arn:aws:lambda:us-east-1:111122223333:function:m")
    monkeypatch.setattr(mc, "_lambda", _FakeLambda({"records": [{"content": "remembered fact"}]}))


def _set(monkeypatch, fake):
    monkeypatch.setattr(mc, "_lambda", fake)
    return fake


# --- opt-in gating ----------------------------------------------------------


def test_disabled_when_no_tool_arn(monkeypatch):
    monkeypatch.setattr(mc, "MEMORY_TOOL_ARN", "")
    assert mc.enabled() is False
    # both ops no-op without touching the client
    assert mc.recall("tok") == []
    assert mc.record("tok", [{"x": 1}], session_id="s") is False


def test_enabled_with_tool_arn():
    assert mc.enabled() is True


# --- recall ------------------------------------------------------------------


def test_recall_forwards_token_and_returns_records(monkeypatch):
    fake = _set(monkeypatch, _FakeLambda({"records": [{"content": "a"}, {"content": "b"}]}))
    recs = mc.recall("verified-token", tier="personal", query="q", session_id="s-1")
    assert [r["content"] for r in recs] == ["a", "b"]
    sent = json.loads(json.loads(fake.calls[0]["Payload"].decode())["body"])
    assert sent["idp_token"] == "verified-token"
    assert sent["op"] == "recall"
    assert sent["tier"] == "personal"


def test_recall_returns_empty_on_non_200(monkeypatch):
    _set(monkeypatch, _FakeLambda({"error": "nope"}, status=403))
    assert mc.recall("tok") == []


def test_recall_never_raises_on_client_error(monkeypatch):
    _set(monkeypatch, _FakeLambda({}, raise_on_invoke=True))
    assert mc.recall("tok") == []  # swallowed


# --- record ------------------------------------------------------------------


def test_record_posts_payload_and_reports_success(monkeypatch):
    fake = _set(monkeypatch, _FakeLambda({"recorded": True}))
    ok = mc.record("tok", [{"type": "answer", "text": "hi"}], session_id="s-1")
    assert ok is True
    sent = json.loads(json.loads(fake.calls[0]["Payload"].decode())["body"])
    assert sent["op"] == "record"
    assert sent["session_id"] == "s-1"
    assert sent["payload"] == [{"type": "answer", "text": "hi"}]


def test_record_noop_without_session_or_payload():
    assert mc.record("tok", [], session_id="s") is False
    assert mc.record("tok", [{"a": 1}], session_id="") is False


def test_record_never_raises(monkeypatch):
    _set(monkeypatch, _FakeLambda({}, raise_on_invoke=True))
    assert mc.record("tok", [{"a": 1}], session_id="s") is False


# --- evidence rendering ------------------------------------------------------


def test_recall_as_evidence_renders_records():
    block = mc.recall_as_evidence([{"content": "fact one"}, {"text": "fact two"}])
    assert "Relevant remembered context:" in block
    assert "- fact one" in block
    assert "- fact two" in block


def test_recall_as_evidence_handles_nested_content():
    block = mc.recall_as_evidence([{"content": {"text": "nested fact"}}])
    assert "- nested fact" in block


def test_recall_as_evidence_empty_on_no_records():
    assert mc.recall_as_evidence([]) == ""
    assert mc.recall_as_evidence([{"content": ""}]) == ""
