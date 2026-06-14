"""Unit tests for the mode router (§10.2.2). Fakes only, no AWS."""

from __future__ import annotations

import pytest
from agate.router import (
    DEFAULT_MODE,
    ROUTER_SYSTEM,
    classify_mode,
    resolve_mode,
    run_router,
)

# --- classify_mode ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("SYNTHESIS", "SYNTHESIS"),
        ("DEBATE", "DEBATE"),
        ("ANALYSIS", "ANALYSIS"),
        ("debate", "DEBATE"),  # casing
        ("Mode: ANALYSIS.", "ANALYSIS"),  # surrounded by noise
        ("  DEBATE\n", "DEBATE"),  # whitespace
    ],
)
def test_classify_exact_tokens(raw, expected):
    assert classify_mode(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("please compute the mean and plot it", "ANALYSIS"),
        ("compare the perspectives", "DEBATE"),
        ("summarize the findings", "SYNTHESIS"),
    ],
)
def test_classify_by_cue_when_no_token(raw, expected):
    assert classify_mode(raw) == expected


def test_classify_ambiguous_defaults_to_cheapest():
    assert classify_mode("hmm not sure") == DEFAULT_MODE == "SYNTHESIS"
    assert classify_mode("") == "SYNTHESIS"


# --- resolve_mode (override precedence) -------------------------------------


def test_override_wins_over_routed():
    assert resolve_mode("SYNTHESIS", "DEBATE") == "DEBATE"
    assert resolve_mode("ANALYSIS", "synthesis") == "SYNTHESIS"  # case-insensitive


def test_no_override_keeps_routed():
    assert resolve_mode("DEBATE", None) == "DEBATE"
    assert resolve_mode("DEBATE", "") == "DEBATE"


def test_invalid_override_ignored():
    assert resolve_mode("DEBATE", "BANANA") == "DEBATE"


# --- run_router orchestration ------------------------------------------------


class FakeBackend:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0
        self.last_max_tokens: int | None = None

    def converse(self, tier, system, prompt, max_tokens):
        self.calls += 1
        self.last_max_tokens = max_tokens
        assert system == ROUTER_SYSTEM
        return self.reply, {"inputTokens": 12, "outputTokens": 1}, None


class FakeMeter:
    def __init__(self):
        self._total = 0.0
        self.labels: list[str] = []

    @property
    def total(self):
        return self._total

    def add_llm(self, label, tier, model_label, usage):
        self._total += 0.00001
        self.labels.append(label)
        return 0.00001


ROUTER = {"tier": "oss", "label": "router", "max_tokens": 5}


def _collect():
    events: list[dict] = []
    return events, events.append


def test_run_router_makes_cheap_metered_call_and_emits_route_not_answer():
    events, emit = _collect()
    backend = FakeBackend("DEBATE")
    meter = FakeMeter()
    mode = run_router(
        backend=backend, meter=meter, emit=emit, question="compare these", router=ROUTER
    )
    assert mode == "DEBATE"
    assert backend.calls == 1
    assert backend.last_max_tokens == 5  # tiny classification call
    # metered on the receipt...
    assert "router" in meter.labels
    assert any(e["type"] == "cost" for e in events)
    # ...emits a route event but NEVER an answer (not rendered as an answer step)
    assert any(e["type"] == "route" and e["mode"] == "DEBATE" for e in events)
    assert not [e for e in events if e["type"] == "answer"]


def test_override_short_circuits_no_call_no_spend():
    events, emit = _collect()
    backend = FakeBackend("DEBATE")  # would route to DEBATE if called
    meter = FakeMeter()
    mode = run_router(
        backend=backend, meter=meter, emit=emit, question="x", router=ROUTER, override="ANALYSIS"
    )
    assert mode == "ANALYSIS"  # user's explicit choice wins
    assert backend.calls == 0  # no routing call made
    assert meter.total == 0.0  # no spend
    assert [e for e in events if e["type"] == "route"][0]["mode"] == "ANALYSIS"


def test_invalid_override_falls_through_to_routing_call():
    events, emit = _collect()
    backend = FakeBackend("ANALYSIS")
    mode = run_router(
        backend=backend,
        meter=FakeMeter(),
        emit=emit,
        question="plot it",
        router=ROUTER,
        override="NONSENSE",
    )
    assert backend.calls == 1  # invalid override ignored -> routing call happens
    assert mode == "ANALYSIS"
