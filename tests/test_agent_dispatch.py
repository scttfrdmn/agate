"""Unit tests for pure agent dispatch (§13.7). Fakes only, no AWS."""

from __future__ import annotations

import json
import threading

import pytest
from agg.agent_dispatch import InvocationError, dispatch
from agg.analyze.schema import ContentBlock, ExecResult

ROSTER = [
    {"tier": "frontier", "label": "frontier", "max_tokens": 256},
    {"tier": "open-weight-70b", "label": "open-weight-70b", "max_tokens": 256},
]
ADJUDICATOR = {"tier": "frontier", "label": "adjudicator", "max_tokens": 512}

GOOD_DIVERGENCE = json.dumps(
    {
        "summary": "Agree on direction.",
        "claims": [
            {
                "id": "c1",
                "text": "Lowers the marker.",
                "kind": "agreement",
                "positions": [{"pane": "frontier", "stance": "supports"}],
                "verify": False,
            }
        ],
    }
)


class FakeBackend:
    """Returns a routing word for the router call, then scripted content."""

    def __init__(
        self, *, route_word: str, adjudication: str = GOOD_DIVERGENCE, answer: str = "ans"
    ):
        self.route_word = route_word
        self.adjudication = adjudication
        self.answer = answer
        self.systems: list[str] = []
        self._lock = threading.Lock()

    def converse(self, tier, system, prompt, max_tokens):
        usage = {"inputTokens": 10, "outputTokens": 2}
        with self._lock:
            self.systems.append(system)
        # Discriminate on a phrase unique to each system prompt (the adjudicator
        # prompt also says "Classify ... claim", so match the router precisely).
        if "Reply with one word" in system:
            return self.route_word, usage, None
        if "You are the adjudicator" in system:
            return self.adjudication, usage, None
        if "writing a single, self-contained Python script" in system:
            return "```python\nprint('x')\n```", usage, None
        return self.answer, usage, None


class FakeMeter:
    def __init__(self):
        self._total = 0.0
        self._lock = threading.Lock()

    @property
    def total(self):
        return self._total

    def add_llm(self, *a):
        with self._lock:
            self._total += 0.001
        return 0.001

    def add_compute(self, label, seconds):
        with self._lock:
            self._total += 0.0001
        return 0.0001


class FakeRunner:
    def execute(self, code, *, language="python"):
        return ExecResult(content=[ContentBlock(type="text", text="42")], elapsed_s=1.0)


def _collect():
    events: list[dict] = []
    lock = threading.Lock()

    def emit(ev):
        with lock:
            events.append(ev)

    return events, emit


def test_dispatch_routes_to_panel_on_debate():
    events, emit = _collect()
    backend = FakeBackend(route_word="DEBATE")
    out = dispatch(
        {"question": "compare", "evidence": "DOC1", "roster": ROSTER, "adjudicator": ADJUDICATOR},
        backend=backend,
        meter=FakeMeter(),
        emit=emit,
    )
    assert out["mode"] == "DEBATE"
    assert any(e["type"] == "divergence" for e in events)
    assert any(e["type"] == "route" and e["mode"] == "DEBATE" for e in events)


def test_dispatch_routes_to_analyze_on_analysis():
    events, emit = _collect()
    backend = FakeBackend(route_word="ANALYSIS")
    out = dispatch(
        {"question": "plot it", "generator": {"tier": "frontier", "label": "g", "max_tokens": 256}},
        backend=backend,
        meter=FakeMeter(),
        emit=emit,
        code_runner=FakeRunner(),
    )
    assert out["mode"] == "ANALYSIS"
    assert any(e["type"] == "code" for e in events)


def test_dispatch_ask_default():
    events, emit = _collect()
    backend = FakeBackend(route_word="SYNTHESIS", answer="The answer.")
    out = dispatch(
        {"question": "what is x?", "evidence": "DOC1"},
        backend=backend,
        meter=FakeMeter(),
        emit=emit,
    )
    assert out["mode"] == "SYNTHESIS"
    answers = [e for e in events if e["type"] == "answer"]
    assert answers and answers[0]["text"] == "The answer."


def test_explicit_override_skips_router():
    events, emit = _collect()
    backend = FakeBackend(route_word="SYNTHESIS")  # would route to Ask
    out = dispatch(
        {
            "question": "x",
            "mode": "DEBATE",
            "evidence": "e",
            "roster": ROSTER,
            "adjudicator": ADJUDICATOR,
        },
        backend=backend,
        meter=FakeMeter(),
        emit=emit,
    )
    assert out["mode"] == "DEBATE"  # override won
    # router system prompt never sent (override short-circuits the routing call)
    assert not any("Reply with one word" in s for s in backend.systems)


def test_missing_question_is_an_error():
    _, emit = _collect()
    with pytest.raises(InvocationError):
        dispatch({}, backend=FakeBackend(route_word="SYNTHESIS"), meter=FakeMeter(), emit=emit)


def test_debate_without_roster_is_an_error():
    _, emit = _collect()
    with pytest.raises(InvocationError):
        dispatch(
            {"question": "x", "mode": "DEBATE"},
            backend=FakeBackend(route_word="DEBATE"),
            meter=FakeMeter(),
            emit=emit,
        )


def test_analysis_without_runner_is_an_error():
    _, emit = _collect()
    with pytest.raises(InvocationError):
        dispatch(
            {"question": "x", "mode": "ANALYSIS"},
            backend=FakeBackend(route_word="ANALYSIS"),
            meter=FakeMeter(),
            emit=emit,
        )


# --- SEC-2: model entitlement enforcement (allowed_models) -------------------


def test_dispatch_rejects_model_outside_allowed_set():
    # An oss-entitled session names a frontier model in the roster -> rejected
    # BEFORE any model call (no router call, no panel).
    _, emit = _collect()
    backend = FakeBackend(route_word="DEBATE")
    with pytest.raises(InvocationError, match="not entitled"):
        dispatch(
            {
                "question": "x",
                "mode": "DEBATE",
                "evidence": "e",
                "roster": ROSTER,
                "adjudicator": ADJUDICATOR,
            },
            backend=backend,
            meter=FakeMeter(),
            emit=emit,
            allowed_models={"open-weight-70b"},  # NOT "frontier"
        )
    # nothing was invoked
    assert backend.systems == []


def test_dispatch_allows_when_all_models_entitled():
    events, emit = _collect()
    backend = FakeBackend(route_word="DEBATE")
    out = dispatch(
        {
            "question": "x",
            "mode": "DEBATE",
            "evidence": "e",
            "roster": ROSTER,
            "adjudicator": ADJUDICATOR,
        },
        backend=backend,
        meter=FakeMeter(),
        emit=emit,
        allowed_models={"frontier", "open-weight-70b"},
    )
    assert out["mode"] == "DEBATE"


def test_dispatch_no_allowed_models_skips_check():
    # allowed_models=None preserves old behavior (tests not exercising entitlement).
    events, emit = _collect()
    out = dispatch(
        {
            "question": "x",
            "mode": "ANALYSIS",
            "generator": {"tier": "frontier", "label": "g", "max_tokens": 64},
        },
        backend=FakeBackend(route_word="ANALYSIS"),
        meter=FakeMeter(),
        emit=emit,
        code_runner=FakeRunner(),
    )
    assert out["mode"] == "ANALYSIS"
