"""Unit tests for run_panel — fakes only, no AWS (§10.2.12 #2).

Covers: per-pane start/done/cost events, the same evidence reaching every member,
a well-formed adjudication emitting a `divergence` whose panes are a subset of the
roster labels, AND the malformed-adjudicator fallback to an unstructured answer.
"""

from __future__ import annotations

import json
import threading

import pytest
from agg.panel.orchestrator import run_panel
from agg.panel.prompts import ADJUDICATE_SYSTEM, REVIEW_SYSTEM

ROSTER = [
    {"tier": "frontier", "label": "frontier", "max_tokens": 512},
    {"tier": "open-weight-70b", "label": "open-weight-70b", "max_tokens": 512},
]
ADJUDICATOR = {"tier": "frontier", "label": "adjudicator", "max_tokens": 1024}

GOOD_ADJUDICATION = json.dumps(
    {
        "summary": "Agree on direction, differ on magnitude.",
        "claims": [
            {
                "id": "c1",
                "text": "Treatment lowers the marker.",
                "kind": "agreement",
                "positions": [
                    {"pane": "frontier", "stance": "supports"},
                    {"pane": "open-weight-70b", "stance": "supports"},
                ],
                "verify": False,
                "evidence_refs": ["DOC1"],
            },
            {
                "id": "c2",
                "text": "The effect is large.",
                "kind": "disagreement",
                "positions": [
                    {"pane": "frontier", "stance": "partial"},
                    {"pane": "open-weight-70b", "stance": "supports"},
                ],
                "verify": True,
            },
        ],
    }
)


class FakeMeter:
    """Thread-safe fake CostMeter. Each call adds a fixed increment."""

    def __init__(self):
        self._total = 0.0
        self._lock = threading.Lock()

    @property
    def total(self) -> float:
        return self._total

    def add_llm(self, label, tier, model_label, usage) -> float:
        inc = 0.001
        with self._lock:
            self._total += inc
        return inc


class FakeBackend:
    """Returns scripted text. Records the prompt each member received so a test can
    assert all members saw the SAME evidence. The adjudicator reply is configurable."""

    def __init__(self, *, adjudication: str):
        self.adjudication = adjudication
        self.review_prompts: list[str] = []
        self.adjudicator_systems: list[str] = []
        self._lock = threading.Lock()

    def converse(self, tier, system, prompt, max_tokens):
        usage = {"inputTokens": 100, "outputTokens": 20}
        if system == ADJUDICATE_SYSTEM:
            with self._lock:
                self.adjudicator_systems.append(system)
            return self.adjudication, usage, None
        # A review call.
        with self._lock:
            self.review_prompts.append(prompt)
        return f"review from {tier}", usage, None


def _collect():
    events: list[dict] = []
    lock = threading.Lock()

    def emit(ev: dict) -> None:
        with lock:
            events.append(ev)

    return events, emit


def _run(adjudication: str):
    events, emit = _collect()
    backend = FakeBackend(adjudication=adjudication)
    meter = FakeMeter()
    result = run_panel(
        backend=backend,
        meter=meter,
        emit=emit,
        question="Does the treatment work?",
        evidence="DOC1: it lowers the marker.",
        roster=ROSTER,
        adjudicator=ADJUDICATOR,
        review_system=REVIEW_SYSTEM,
        adjudicate_system=ADJUDICATE_SYSTEM,
    )
    return events, backend, meter, result


def test_emits_start_and_done_pane_per_member():
    events, _, _, _ = _run(GOOD_ADJUDICATION)
    starts = [e for e in events if e["type"] == "model" and e["state"] == "start"]
    dones = [e for e in events if e["type"] == "model" and e["state"] == "done"]
    assert {e["pane"] for e in starts} == {"frontier", "open-weight-70b"}
    assert {e["pane"] for e in dones} == {"frontier", "open-weight-70b"}
    # each done carries per-pane cost + usage
    for e in dones:
        assert e["cost"] > 0
        assert e["usage"]["outputTokens"] == 20


def test_every_member_sees_the_same_evidence():
    _, backend, _, _ = _run(GOOD_ADJUDICATION)
    assert len(backend.review_prompts) == len(ROSTER)
    assert len(set(backend.review_prompts)) == 1  # identical prompt to all members
    assert "DOC1: it lowers the marker." in backend.review_prompts[0]


def test_well_formed_adjudication_emits_divergence_with_roster_panes():
    events, _, _, result = _run(GOOD_ADJUDICATION)
    divs = [e for e in events if e["type"] == "divergence"]
    assert len(divs) == 1
    div = divs[0]
    # (a) panes are a subset of roster labels (the spec's assertion)
    roster_labels = {m["label"] for m in ROSTER}
    panes = {p["pane"] for c in div["claims"] for p in c["positions"]}
    assert panes <= roster_labels
    # the reconciled summary is also surfaced as an answer
    answers = [e for e in events if e["type"] == "answer"]
    assert any("differ on magnitude" in a["text"] for a in answers)
    # structured payload is returned under __adjudication__
    assert result["__adjudication__"]["claims"][1]["kind"] == "disagreement"


def test_cost_events_accumulate():
    events, _, meter, _ = _run(GOOD_ADJUDICATION)
    costs = [e for e in events if e["type"] == "cost"]
    assert costs, "expected running cost events"
    # totals are non-decreasing
    totals = [c["total"] for c in costs]
    assert totals == sorted(totals)
    # final meter total reflects N reviews + 1 adjudication
    assert meter.total == pytest.approx(0.001 * (len(ROSTER) + 1))


# --- the malformed-adjudicator fallback path (spec-required) ----------------


def test_malformed_json_falls_back_to_unstructured_answer():
    events, _, _, result = _run("this is not JSON {oops")
    # No divergence event; an unstructured answer carries the raw text.
    assert not [e for e in events if e["type"] == "divergence"]
    fallback = [e for e in events if e["type"] == "answer"]
    assert len(fallback) == 1
    assert "unstructured" in fallback[0]["title"].lower()
    assert "not JSON" in fallback[0]["text"]
    # result still returns a usable shape (no exception raised mid-run)
    assert result["__adjudication__"]["claims"] == []


def test_schema_invalid_json_falls_back():
    # Valid JSON, but violates the Divergence schema (bad stance enum).
    bad = json.dumps(
        {
            "summary": "x",
            "claims": [
                {
                    "id": "c1",
                    "text": "t",
                    "kind": "agreement",
                    "positions": [{"pane": "frontier", "stance": "maybe"}],
                    "verify": False,
                }
            ],
        }
    )
    events, _, _, _ = _run(bad)
    assert not [e for e in events if e["type"] == "divergence"]
    assert any(e["type"] == "answer" and "unstructured" in e["title"].lower() for e in events)


def test_adjudication_with_markdown_fences_still_parses():
    fenced = f"```json\n{GOOD_ADJUDICATION}\n```"
    events, _, _, _ = _run(fenced)
    assert len([e for e in events if e["type"] == "divergence"]) == 1


def test_single_member_panel_is_not_degenerate_error():
    # A one-member roster still runs (degenerate but must not crash).
    events, emit = _collect()
    run_panel(
        backend=FakeBackend(adjudication=GOOD_ADJUDICATION),
        meter=FakeMeter(),
        emit=emit,
        question="q",
        evidence="e",
        roster=[{"tier": "frontier", "label": "solo", "max_tokens": 10}],
        adjudicator=ADJUDICATOR,
        review_system=REVIEW_SYSTEM,
        adjudicate_system=ADJUDICATE_SYSTEM,
    )
    assert any(e["type"] == "model" and e.get("pane") == "solo" for e in events)
