"""Tests for the runtime→governed-tool research bridge (#248). No AWS — the Lambda client stubbed.

The bridge forwards the verified token to the web-search / web-fetch tool Lambdas and never reaches
the web itself. Opt-in + fail-closed: an unwired search edge returns [], an unwired/failed fetch
edge RAISES (the loop must never treat unfetched bytes as evidence).
"""

from __future__ import annotations

import json

import pytest
from agent import research_client as rc


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
def _wired(monkeypatch):
    monkeypatch.setattr(rc, "WEBSEARCH_TOOL_ARN", "arn:aws:lambda:us-east-1:1:function:s")
    monkeypatch.setattr(rc, "WEBFETCH_TOOL_ARN", "arn:aws:lambda:us-east-1:1:function:f")


def _set(monkeypatch, fake):
    monkeypatch.setattr(rc, "_lambda", fake)
    return fake


# --- search edge ------------------------------------------------------------


def test_search_forwards_token_and_returns_urls(monkeypatch):
    fake = _set(monkeypatch, _FakeLambda({"results": ["https://a/1", "https://a/2"]}))
    search = rc.make_search("verified-token")
    assert search("kinetics") == ["https://a/1", "https://a/2"]
    sent = json.loads(json.loads(fake.calls[0]["Payload"].decode())["body"])
    assert sent["idp_token"] == "verified-token"
    assert sent["tool"] == "web-search"
    assert sent["query"] == "kinetics"


def test_search_returns_empty_when_unwired(monkeypatch):
    monkeypatch.setattr(rc, "WEBSEARCH_TOOL_ARN", "")
    assert rc.make_search("tok")("q") == []


def test_search_returns_empty_on_tool_error(monkeypatch):
    _set(monkeypatch, _FakeLambda({"error": "nope"}, status=403))
    assert rc.make_search("tok")("q") == []


def test_search_never_raises_on_client_error(monkeypatch):
    _set(monkeypatch, _FakeLambda({}, raise_on_invoke=True))
    assert rc.make_search("tok")("q") == []


# --- fetch edge (fails CLOSED — raises, never returns unvalidated bytes) ----


def test_fetch_forwards_token_and_returns_content(monkeypatch):
    fake = _set(monkeypatch, _FakeLambda({"content": "paper body", "url": "https://a/1"}))
    fetch = rc.make_fetch("verified-token")
    assert fetch("https://a/1") == "paper body"
    sent = json.loads(json.loads(fake.calls[0]["Payload"].decode())["body"])
    assert sent["idp_token"] == "verified-token"
    assert sent["tool"] == "web-fetch"
    assert sent["url"] == "https://a/1"


def test_fetch_raises_when_unwired(monkeypatch):
    monkeypatch.setattr(rc, "WEBFETCH_TOOL_ARN", "")
    with pytest.raises(rc.ResearchFetchError):
        rc.make_fetch("tok")("https://a/1")


def test_fetch_raises_on_tool_error(monkeypatch):
    _set(monkeypatch, _FakeLambda({"error": "blocked"}, status=403))
    with pytest.raises(rc.ResearchFetchError):
        rc.make_fetch("tok")("https://a/1")


def test_fetch_raises_when_no_content_key(monkeypatch):
    _set(monkeypatch, _FakeLambda({"url": "https://a/1"}))  # 200 but no content
    with pytest.raises(rc.ResearchFetchError):
        rc.make_fetch("tok")("https://a/1")
