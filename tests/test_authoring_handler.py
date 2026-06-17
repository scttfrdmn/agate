"""Unit tests for the graphical authoring endpoint (#117). No AWS — token stubbed.

The §8.5 thesis on the live surface: graphical authoring is the SAFEST surface. The bounded
menu (`options`) offers only the author's own scope + contained course nodes and tiers ≤ the
author's; `dispose` funnels a builder-assembled spec through the SAME compiler clamp an LLM
draft uses — so a hand-crafted over-broad selection is clamped or rejected. No model, no write.
"""

from __future__ import annotations

import json

import pytest
from infra.functions.authoring import handler as h


def _claims(scope="chemistry", courses=("chem-101", "chem-202"), grant=True):
    return {
        "sub": "prof",
        "affiliation": "researcher",
        "tenant": "uni",
        "data_scope": scope,
        "courses": list(courses),
        "grant": grant,
    }


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims() if tok else _raise())
    return monkeypatch


def _raise():
    raise h.AuthoringError("missing idp_token")


def _invoke(req: dict) -> dict:
    resp = h.handler({"body": json.dumps(req)}, None)
    return {"status": resp["statusCode"], "body": json.loads(resp["body"])}


_VALID_SPEC = {
    "agent": "paper-sweep",
    "description": "summarize new papers",
    "role": "researcher",
    "scope": "chemistry/chem-101",
    "reasoning": "lit-review",
    "tools": ["library-search"],
}


# --- options: the bounded menu ----------------------------------------------


def test_options_offers_own_scope_and_contained_courses(stub):
    out = _invoke({"idp_token": "t", "op": "options"})
    assert out["status"] == 200
    scopes = out["body"]["options"]["offerable_scopes"]
    # own scope + the two courses as sub-nodes under it (containment-filtered)
    assert "chemistry" in scopes
    assert "chemistry/chem-101" in scopes
    assert "chemistry/chem-202" in scopes
    # never a sibling/disjoint scope
    assert "physics" not in scopes


def test_options_never_offers_a_tier_above_the_author(stub, monkeypatch):
    # A student author (oss tier, no grant) is offered only oss — never a higher tier.
    student = {**_claims(grant=False), "affiliation": "student"}
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: student)
    out = _invoke({"idp_token": "t", "op": "options"})
    tiers = out["body"]["options"]["offerable_tiers"]
    assert tiers == ["oss"]
    assert "frontier" not in tiers


def test_options_includes_catalogs_and_templates(stub):
    out = _invoke({"idp_token": "t", "op": "options"})
    opts = out["body"]["options"]
    assert opts["capabilities"] and opts["patterns"]
    assert any(t["id"] == "paper-monitor" for t in out["body"]["templates"])


def test_options_is_the_default_op(stub):
    # No op -> options (the builder's first call).
    out = _invoke({"idp_token": "t"})
    assert "options" in out["body"]


# --- dispose: the compiler clamp (the boundary) -----------------------------


def test_dispose_within_reach_returns_plan_and_spec(stub):
    out = _invoke({"idp_token": "t", "op": "dispose", "spec": _VALID_SPEC})
    assert out["status"] == 200
    assert out["body"]["ok"] is True
    assert " ".join(out["body"]["plan"])  # a legible bounded plan
    assert out["body"]["spec"] == _VALID_SPEC  # echoed for deploy-on-confirm


def test_dispose_clamps_a_hand_crafted_over_broad_selection(stub, monkeypatch):
    # A client bypasses the bounded UI and POSTs another tenant-tree scope -> rejected.
    out = _invoke({"idp_token": "t", "op": "dispose", "spec": {**_VALID_SPEC, "scope": "physics"}})
    assert out["status"] == 200
    assert out["body"]["ok"] is False
    assert "spec" not in out["body"]  # nothing to confirm


def test_dispose_broader_scope_clamps_down(stub, monkeypatch):
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims(scope="chemistry/chem-101"))
    out = _invoke(
        {"idp_token": "t", "op": "dispose", "spec": {**_VALID_SPEC, "scope": "chemistry"}}
    )
    assert out["body"]["ok"] is True
    assert "chemistry/chem-101" in " ".join(out["body"]["plan"])


def test_dispose_template_overlay(stub):
    # A template id fetches the skeleton; the author's slots overlay it.
    out = _invoke(
        {
            "idp_token": "t",
            "op": "dispose",
            "template": "paper-monitor",
            "spec": {"scope": "chemistry/chem-101"},
        }
    )
    assert out["body"]["ok"] is True
    assert out["body"]["spec"]["agent"] == "paper-monitor"
    assert out["body"]["spec"]["scope"] == "chemistry/chem-101"


def test_dispose_unknown_template_fails_closed(stub):
    out = _invoke({"idp_token": "t", "op": "dispose", "template": "no-such", "spec": {}})
    assert out["status"] == 403


def test_dispose_invalid_spec_fails_closed(stub):
    out = _invoke(
        {"idp_token": "t", "op": "dispose", "spec": {**_VALID_SPEC, "tools": ["no-such-tool"]}}
    )
    assert out["status"] == 200
    assert out["body"]["ok"] is False


# --- fail closed ------------------------------------------------------------


def test_missing_token_is_403(stub):
    out = _invoke({"op": "options"})
    assert out["status"] == 403


def test_unknown_op_fails_closed(stub):
    out = _invoke({"idp_token": "t", "op": "delete-everything"})
    assert out["status"] == 403
