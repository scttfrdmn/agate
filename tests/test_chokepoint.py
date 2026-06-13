"""Tests for the Tier 1 choke point — no AWS (spend/budget/STS/Bedrock stubbed).

Post-SEC-1: identity is derived from the IdP token (claims_to_tags), and budget is
looked up server-side — never from request fields. Tests prove a malicious body
cannot forge tenant/tier/budget.
"""

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
    def __init__(self, br: _FakeBedrock):
        self._br = br
        self.spend = 0.0
        self.budget: float | None = 100.0
        self.assumed_tags = None  # records the SessionTags assume_user_role got

    @property
    def calls(self) -> int:
        return self._br.calls


@pytest.fixture
def wired(monkeypatch):
    fake_br = _FakeBedrock()
    w = _Wired(fake_br)

    def fake_assume(tags, user):
        w.assumed_tags = tags
        return fake_br

    monkeypatch.setattr(cp, "assume_user_role", fake_assume)
    monkeypatch.setattr(cp, "read_spend", lambda tenant, user, period: w.spend)
    monkeypatch.setattr(cp, "lookup_budget", lambda tenant, user, period: w.budget)
    monkeypatch.setattr(cp, "AUTHENTICATED_ROLE_ARN", "arn:aws:iam::123:role/agg-authenticated")

    # Simulate a VERIFIED token: decode the JSON the test passes as `idp_token`.
    # Real signature/JWKS verification is covered by tests/test_jwt_verify.py.
    def _decode(token):
        if not token:
            raise cp.ChokepointError("no token")
        try:
            claims = json.loads(token)
        except ValueError as exc:
            raise cp.ChokepointError("bad token") from exc
        if not isinstance(claims, dict):
            raise cp.ChokepointError("bad token")
        return claims

    monkeypatch.setattr(cp, "validate_idp_token", _decode)
    return w


# A valid (Phase-1 placeholder) IdP token = pre-validated claims JSON, same as broker.
def _token(affiliation="student", tenant="chem", sub="student-7", **extra):
    claims = {"sub": sub, "affiliation": affiliation, "tenant": tenant, **extra}
    return json.dumps(claims)


def _req(token=None, **over):
    r = {
        "idp_token": token if token is not None else _token(),
        "model": "oss",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 1000,
    }
    r.update(over)
    return r


def test_allows_and_invokes_when_within_budget(wired):
    wired.spend, wired.budget = 1.0, 100.0
    out = cp.process(_req(), period="2026-06")
    assert out["text"] == "answer"
    assert wired.calls == 1
    # the session was scoped by the TOKEN-derived tags
    sent = {t["Key"]: t["Value"] for t in wired.assumed_tags.to_sts_tags()}
    assert sent["agg:tenant"] == "chem"
    assert sent["agg:tier"] == "oss"


def test_rejects_pre_call_when_over_budget(wired):
    wired.spend, wired.budget = 99.999, 100.0
    with pytest.raises(cp.ChokepointError, match="budget"):
        cp.process(_req(model="frontier", max_tokens=1000), period="2026-06")
    assert wired.calls == 0  # model NOT invoked


def test_zero_budget_rejects_before_call(wired):
    wired.spend, wired.budget = 0.0, 0.0
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(), period="2026-06")
    assert wired.calls == 0


def test_no_budget_configured_allows(wired):
    wired.spend, wired.budget = 9999.0, None
    out = cp.process(_req(), period="2026-06")
    assert out["text"] == "answer"


# --- SEC-1 regression: the body cannot forge identity or budget ---------------


def test_body_cannot_forge_tenant_or_tier(wired):
    wired.spend, wired.budget = 1.0, 100.0
    # Malicious body claims tenant=law, tier=frontier, budget=1e9 — all ignored.
    out = cp.process(
        _req(tenant="law", user="victim", tier="frontier", courses=["x"], budget=1e9),
        period="2026-06",
    )
    assert out["text"] == "answer"
    sent = {t["Key"]: t["Value"] for t in wired.assumed_tags.to_sts_tags()}
    # tenant/tier come from the TOKEN (chem/oss), not the body (law/frontier)
    assert sent["agg:tenant"] == "chem"
    assert sent["agg:tier"] == "oss"


def test_body_budget_field_is_ignored(wired):
    # Caller sends a huge body budget but the server budget is tiny -> reject.
    wired.spend, wired.budget = 0.5, 0.4
    with pytest.raises(cp.ChokepointError, match="budget"):
        cp.process(_req(model="frontier", budget=1e9, max_tokens=1000), period="2026-06")
    assert wired.calls == 0


def test_missing_or_bad_token_fails_closed(wired):
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(token=""), period="2026-06")
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(token="not json"), period="2026-06")


def test_token_without_tenant_fails_closed(wired):
    # claims_to_tags raises ClaimsError on a missing tenant -> ChokepointError.
    bad = json.dumps({"sub": "u1", "affiliation": "student"})  # no tenant
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(token=bad), period="2026-06")
    assert wired.calls == 0


def test_missing_model_or_messages_rejected(wired):
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(model=None), period="2026-06")
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(messages=[]), period="2026-06")


def test_estimate_input_tokens_is_server_side():
    # char/4 + 1, conservative round-up; no client override accepted.
    assert cp.estimate_input_tokens([{"role": "user", "content": "x" * 40}]) == 11
    assert cp.estimate_input_tokens([]) == 1


def test_handler_maps_reject_to_402(wired):
    wired.spend, wired.budget = 0.0, 0.0
    resp = cp.handler({"body": json.dumps(_req())}, None)
    assert resp["statusCode"] == 402
    assert "budget_rejected" in resp["body"]


def test_handler_200_on_allow(wired):
    wired.spend, wired.budget = 0.0, 100.0
    resp = cp.handler({"body": json.dumps(_req())}, None)
    assert resp["statusCode"] == 200
    assert "answer" in resp["body"]
