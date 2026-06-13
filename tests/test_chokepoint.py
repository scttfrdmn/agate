"""Tests for the Tier 1 choke point — no AWS (spend table + STS + Bedrock stubbed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chokepoint import handler as cp  # noqa: E402


class _FakeBedrock:
    def __init__(self):
        self.calls = 0

    def converse(self, modelId, messages, inferenceConfig):  # noqa: N803
        self.calls += 1
        return {
            "output": {"message": {"content": [{"text": "answer"}]}},
            "usage": {"inputTokens": 12, "outputTokens": 8},
        }


class _Wired:
    """Holds the stubbed Bedrock client + a mutable spend the test can set."""

    def __init__(self, br: _FakeBedrock):
        self._br = br
        self.spend = 0.0

    @property
    def calls(self) -> int:
        return self._br.calls


@pytest.fixture
def wired(monkeypatch):
    fake_br = _FakeBedrock()
    w = _Wired(fake_br)
    monkeypatch.setattr(cp, "assume_user_role", lambda *a, **k: fake_br)
    monkeypatch.setattr(cp, "read_spend", lambda tenant, user, period: w.spend)
    monkeypatch.setattr(cp, "AUTHENTICATED_ROLE_ARN", "arn:aws:iam::123:role/agg-authenticated")
    return w


def _req(**over):
    r = {
        "tenant": "chem",
        "user": "student-7",
        "period": "2026-06",
        "model": "oss",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 1000,
        "tier": "oss",
        "budget": 100.0,
    }
    r.update(over)
    return r


def test_allows_and_invokes_when_within_budget(wired):
    wired.spend = 1.0
    out = cp.process(_req())
    assert out["text"] == "answer"
    assert out["usage"]["outputTokens"] == 8
    assert wired.calls == 1


def test_rejects_pre_call_when_over_budget(wired):
    wired.spend = 99.999  # almost exhausted; worst-case call pushes over
    with pytest.raises(cp.ChokepointError, match="budget"):
        cp.process(_req(model="frontier", input_tokens=1_000_000, max_tokens=1000))
    # crucially, the model was NOT invoked
    assert wired.calls == 0


def test_zero_budget_rejects_before_call(wired):
    wired.spend = 0.0
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(budget=0.0))
    assert wired.calls == 0


def test_no_budget_allows(wired):
    wired.spend = 9999.0
    out = cp.process(_req(budget=None))
    assert out["text"] == "answer"


def test_missing_fields_rejected(wired):
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(tenant=None))
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(messages=[]))


def test_estimate_input_tokens_char_heuristic():
    # ~ char/4 + 1, conservative round-up
    n = cp.estimate_input_tokens([{"role": "user", "content": "x" * 40}], None)
    assert n == 11
    # explicit value trusted
    assert cp.estimate_input_tokens([], 50) == 50


def test_handler_maps_reject_to_402(wired):
    wired.spend = 0.0
    event = {"body": json.dumps(_req(budget=0.0))}
    resp = cp.handler(event, None)
    assert resp["statusCode"] == 402
    assert "budget_rejected" in resp["body"]


def test_handler_200_on_allow(wired):
    wired.spend = 0.0
    resp = cp.handler({"body": json.dumps(_req())}, None)
    assert resp["statusCode"] == 200
    assert "answer" in resp["body"]
