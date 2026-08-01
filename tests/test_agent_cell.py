"""Tests for the container-side agent-cell runner (#248). No AWS — backend + edges stubbed.

Exercises the wiring `agent/server.py` routes to for a capped payload: cap parsing, the
Bedrock-backed planner (a JSON-action reply), the governed edges, and the terminal receipt. The
enforcement itself is proven in test_research_loop; here we assert the container assembles it
correctly and fails closed on an ungoverned launch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import agent_cell, research_client  # noqa: E402


class ScriptedBackend:
    """Returns successive canned Converse replies (the planner's JSON actions)."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def converse(self, tier, system, prompt, max_tokens):
        i = min(self.calls, len(self._replies) - 1)
        self.calls += 1
        return self._replies[i], {"inputTokens": 10, "outputTokens": 3}, None


SCOPE_OK = [("tenant", 0.0, 1000.0)]


def _run(payload, backend, monkeypatch, *, scope_reader=None, search=None, fetch=None):
    # Stub the governed edges so no Lambda is invoked.
    monkeypatch.setattr(research_client, "make_search", lambda tok: search or (lambda q: []))
    monkeypatch.setattr(research_client, "make_fetch", lambda tok: fetch or (lambda u: "content"))
    events: list[dict] = []
    agent_cell.run_agent_cell(
        payload,
        backend=backend,
        tier="oss",
        entitled=["oss"],
        emit=events.append,
        clock=lambda: 0.0,
        scope_reader=scope_reader,
    )
    return events


def test_answers_within_envelope_and_emits_receipt(monkeypatch):
    backend = ScriptedBackend(['{"action":"answer","text":"the answer"}'])
    events = _run(
        {"question": "q", "cap": {"cost_usd": 2.0}, "idp_token": "t"},
        backend,
        monkeypatch,
        scope_reader=lambda tenant, scope: SCOPE_OK,
    )
    answers = [e for e in events if e["type"] == "answer"]
    assert answers[0]["text"] == "the answer"
    assert events[-1]["type"] == "receipt"
    assert events[-1]["kind"] == "agent-cell"
    assert events[-1]["answer_cap_bounded"] is False


def test_refuses_ungoverned_launch(monkeypatch):
    # All-None cap AND no scope floor → the loop refuses; the runner surfaces a fail-closed error.
    backend = ScriptedBackend(['{"action":"answer","text":"should not run"}'])
    events = _run(
        {"question": "q", "cap": {}, "idp_token": "t"},
        backend,
        monkeypatch,
        scope_reader=lambda tenant, scope: [],
    )
    assert events[0]["title"] == "error"
    assert "refused" in events[0]["text"]
    assert backend.calls == 0  # the planner was never invoked


def test_cost_cap_bounds_a_greedy_planner(monkeypatch):
    # The planner always wants another search; with planning free ($0), the $0.012 cap allows two
    # $0.005 searches, not three. (Planning cost is exercised separately below.)
    monkeypatch.setattr(agent_cell, "PLAN_PRICE_USD", 0.0)
    backend = ScriptedBackend(['{"action":"search","query":"more"}'])  # same reply every turn
    events = _run(
        {"question": "q", "cap": {"cost_usd": 0.012}, "idp_token": "t"},
        backend,
        monkeypatch,
        scope_reader=lambda tenant, scope: SCOPE_OK,
        search=lambda q: ["https://a/1"],
    )
    receipt = events[-1]
    assert receipt["answer_cap_bounded"] is True
    # 0.005 * 2 = 0.010 <= 0.012; the third (0.015) is refused.
    assert receipt["spent_usd"] == pytest.approx(0.010)


def test_planner_model_cost_counts_against_the_cap(monkeypatch):
    # Review PR3 Finding 1: the reasoning-model turn is priced (PLAN_PRICE_USD) and gated against
    # the cap. With a $0.05 cap and $0.02/turn, only 2 planning turns fit before the run stops —
    # even if the planner keeps issuing free searches.
    monkeypatch.setattr(agent_cell, "PLAN_PRICE_USD", 0.02)
    backend = ScriptedBackend(['{"action":"search","query":"more"}'])
    events = _run(
        {"question": "q", "cap": {"cost_usd": 0.05}, "idp_token": "t"},
        backend,
        monkeypatch,
        scope_reader=lambda tenant, scope: SCOPE_OK,
        search=lambda q: ["https://a/1"],
    )
    receipt = events[-1]
    assert receipt["answer_cap_bounded"] is True
    assert "before planning" in receipt["stop_reason"]
    assert backend.calls == 2  # 2 * 0.02 = 0.04 <= 0.05; the 3rd planning turn is gated out


def test_missing_question_fails_closed(monkeypatch):
    events = _run({"cap": {"cost_usd": 1.0}}, ScriptedBackend(["x"]), monkeypatch)
    assert events[0]["title"] == "error"


def test_cap_parsing_coerces_types():
    cap = agent_cell._cap_from_payload({"cost_usd": "2.5", "seconds": "300", "max_steps": "4"})
    assert cap.cost_usd == 2.5 and cap.seconds == 300.0 and cap.max_steps == 4
    # Garbled cap → all-None (which the loop then refuses unless scope makes it enforceable).
    garbled = agent_cell._cap_from_payload({"cost_usd": "abc"})
    assert garbled.cost_usd is None
    assert agent_cell._cap_from_payload("not a dict") == agent_cell.AgentCellCap()


def test_cap_parsing_survives_non_finite_max_steps(monkeypatch):
    # Review PR3 Finding 2: a JSON non-finite reaching int() (1e400 → inf → OverflowError) must not
    # escape as a 500 — it coerces to None (all-None cap), which the loop then refuses.
    cap = agent_cell._cap_from_payload({"max_steps": float("inf")})
    assert cap.max_steps is None
    # End to end: such a cap + no scope floor is refused, not crashed.
    events = _run(
        {"question": "q", "cap": {"max_steps": float("inf")}, "idp_token": "t"},
        ScriptedBackend(["x"]),
        monkeypatch,
        scope_reader=lambda tenant, scope: [],
    )
    assert events[0]["title"] == "error"
    assert "refused" in events[0]["text"]


def test_no_entitled_model_fails_closed():
    # An unverifiable token yields no entitled model → fail closed, never a bare-label model call.
    events: list[dict] = []
    agent_cell.run_agent_cell(
        {"question": "q", "cap": {"cost_usd": 1.0}, "idp_token": "t"},
        backend=ScriptedBackend(["x"]),
        tier="oss",
        entitled=[],
        emit=events.append,
        clock=lambda: 0.0,
        scope_reader=lambda tenant, scope: SCOPE_OK,
    )
    assert events[0]["title"] == "error"
    assert "no entitled model" in events[0]["text"]


def test_planner_error_ends_run_not_crash(monkeypatch):
    class BoomBackend:
        def converse(self, *a, **k):
            raise RuntimeError("bedrock down")

    events = _run(
        {"question": "q", "cap": {"cost_usd": 1.0}, "idp_token": "t"},
        BoomBackend(),
        monkeypatch,
        scope_reader=lambda tenant, scope: SCOPE_OK,
    )
    # The planner error becomes the answer text; the run still closes with a receipt.
    assert any("planner error" in e.get("text", "") for e in events)
    assert events[-1]["type"] == "receipt"
