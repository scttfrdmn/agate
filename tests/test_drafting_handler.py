"""Unit tests for the natural-language drafting endpoint (#118b). No AWS — Bedrock stubbed.

The §8.5/§10 thesis on the live surface: the model proposes a spec, the compiler disposes. The
load-bearing assertions — a draft within the author's reach renders a bounded plan; an
over-broad/cross-tenant draft is clamped or rejected (never widened); the model's output is
fence-stripped + parsed but carries ZERO authority; the per-session tier is the verified one;
and everything fails closed.
"""

from __future__ import annotations

import json

import pytest
from agate.entitlements import models_for_tier
from infra.functions.drafting import handler as h


def _claims(tier_aff="researcher", tenant="uni", scope="chemistry"):
    # claims_to_tags derives tier from affiliation+grant; researcher -> a real tier. The data
    # scope comes from the data_scope claim. sub is the subject.
    return {
        "sub": "prof",
        "affiliation": tier_aff,
        "tenant": tenant,
        "data_scope": scope,
        "grant": True,  # promote to frontier so a frontier author is exercised
    }


class _FakeBedrock:
    """Returns a scripted Converse response; records the modelId it was asked for."""

    def __init__(self, text: str):
        self._text = text
        self.calls = []

    def converse(self, **kw):
        self.calls.append(kw)
        return {
            "output": {"message": {"content": [{"text": self._text}]}},
            "usage": {"inputTokens": 10, "outputTokens": 20},
        }


_VALID_DRAFT = {
    "agent": "paper-sweep",
    "description": "summarize new papers",
    "role": "researcher",
    "scope": "chemistry/chem-101",
    "reasoning": "lit-review",
    "tools": ["library-search"],
}


@pytest.fixture
def stub(monkeypatch):
    fb = _FakeBedrock(json.dumps(_VALID_DRAFT))
    monkeypatch.setattr(h, "_bedrock", fb)
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims() if tok else _raise())
    return fb


def _raise():
    raise h.DraftingError("missing idp_token")


def _invoke(req: dict) -> dict:
    resp = h.handler({"body": json.dumps(req)}, None)
    return {"status": resp["statusCode"], "body": json.loads(resp["body"])}


# --- happy path: draft within reach renders a bounded plan ------------------


def test_draft_within_reach_returns_bounded_plan(stub):
    out = _invoke({"idp_token": "t", "request": "summarize new chem papers weekly"})
    assert out["status"] == 200
    assert out["body"]["ok"] is True
    plan = " ".join(out["body"]["plan"])
    assert "chemistry" in plan.lower()
    # the credential/instance is NEVER returned to the client — only the plan
    assert "instance" not in out["body"]
    assert "child_tags" not in json.dumps(out["body"])


def test_drafts_with_the_verified_tier_model(stub):
    # The model id used is the author's cheapest entitled model — tier enforced in code.
    _invoke({"idp_token": "t", "request": "x"})
    model_id = stub.calls[0]["modelId"]
    assert model_id == models_for_tier("frontier")[0]  # grant=True -> frontier author


# --- the headline: the model's draft carries zero authority -----------------


def test_cross_tenant_draft_is_rejected_not_widened(stub, monkeypatch):
    # The model drafts a scope in ANOTHER tenant's tree -> disjoint from the author -> rejected.
    monkeypatch.setattr(
        h, "_bedrock", _FakeBedrock(json.dumps({**_VALID_DRAFT, "scope": "physics"}))
    )
    out = _invoke({"idp_token": "t", "request": "read the physics lab's data"})
    assert out["status"] == 200
    assert out["body"]["ok"] is False  # clamped/rejected — nothing compiles
    assert "outside your own" in out["body"]["reason"] or "clamp" in out["body"]["reason"].lower()


def test_broader_draft_scope_clamps_down(stub, monkeypatch):
    # author at chemistry/chem-101, model drafts the broader 'chemistry' -> clamps DOWN.
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims(scope="chemistry/chem-101"))
    monkeypatch.setattr(
        h, "_bedrock", _FakeBedrock(json.dumps({**_VALID_DRAFT, "scope": "chemistry"}))
    )
    out = _invoke({"idp_token": "t", "request": "broaden me"})
    assert out["body"]["ok"] is True
    assert "chemistry/chem-101" in " ".join(out["body"]["plan"])


# --- model output handling --------------------------------------------------


def test_fenced_json_is_stripped_and_parsed(stub, monkeypatch):
    fenced = "```json\n" + json.dumps(_VALID_DRAFT) + "\n```"
    monkeypatch.setattr(h, "_bedrock", _FakeBedrock(fenced))
    out = _invoke({"idp_token": "t", "request": "x"})
    assert out["body"]["ok"] is True


def test_non_json_model_output_is_a_clean_outcome_not_500(stub, monkeypatch):
    monkeypatch.setattr(h, "_bedrock", _FakeBedrock("Sure! Here is an agent that..."))
    out = _invoke({"idp_token": "t", "request": "x"})
    assert out["status"] == 200
    assert out["body"]["ok"] is False
    assert "valid spec" in out["body"]["reason"]


def test_malformed_draft_dict_fails_closed(stub, monkeypatch):
    # Valid JSON but a spec parse_spec rejects (unknown tool) -> ok=False, never 500.
    monkeypatch.setattr(
        h, "_bedrock", _FakeBedrock(json.dumps({**_VALID_DRAFT, "tools": ["no-such-tool"]}))
    )
    out = _invoke({"idp_token": "t", "request": "x"})
    assert out["status"] == 200
    assert out["body"]["ok"] is False


# --- fail closed ------------------------------------------------------------


def test_missing_token_is_403(stub):
    out = _invoke({"request": "x"})
    assert out["status"] == 403
    assert stub.calls == []  # never reached the model


def test_missing_request_is_403(stub):
    out = _invoke({"idp_token": "t"})
    assert out["status"] == 403
    assert stub.calls == []


def test_extract_json_helpers():
    assert h._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert h._extract_json('{"a": 2}') == {"a": 2}
    assert h._extract_json("not json") is None
    assert h._extract_json("[1, 2]") is None  # a list is not a spec dict
    assert h._extract_json("") is None
