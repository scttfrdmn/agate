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


# --- model axis (#122): entitlement-and-budget-aware auto mode --------------

from agate.entitlements import models_for_tier  # noqa: E402
from agate.router import (  # noqa: E402
    DIFFICULTY_SYSTEM,
    ModelChoice,
    classify_difficulty,
    resolve_model,
    run_model_router,
    select_model,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("SIMPLE", "SIMPLE"),
        ("hard", "HARD"),
        ("Difficulty: MODERATE.", "MODERATE"),
        ("please prove this theorem rigorously", "HARD"),  # cue
        ("what is the capital", "SIMPLE"),  # cue
        ("mumble", "SIMPLE"),  # ambiguous -> cheapest default
    ],
)
def test_classify_difficulty(raw, expected):
    assert classify_difficulty(raw) == expected


def test_select_model_never_exceeds_entitled_tier():
    # The headline: an oss session's candidate set is oss-only, on ANY difficulty/policy.
    oss = set(models_for_tier("oss"))
    for diff in ("SIMPLE", "MODERATE", "HARD"):
        for pol in ("thrifty", "best"):
            c = select_model(tier="oss", difficulty=diff, policy=pol)
            assert c.model_id in oss  # never a mid/frontier id


def test_thrifty_simple_is_cheapest():
    c = select_model(tier="frontier", difficulty="SIMPLE", policy="thrifty")
    assert c.model_id == models_for_tier("frontier")[0]  # cheapest entitled


def test_best_picks_most_capable_affordable():
    c = select_model(tier="frontier", difficulty="SIMPLE", policy="best")
    assert c.model_id == models_for_tier("frontier")[-1]  # most capable, despite SIMPLE


def test_thrifty_and_best_differ_on_hard():
    # thrifty HARD climbs to the top of the affordable set; best is always the top —
    # they coincide on HARD but diverge on SIMPLE (covered above). Assert HARD picks high.
    t = select_model(tier="frontier", difficulty="HARD", policy="thrifty")
    assert t.model_id == models_for_tier("frontier")[-1]


def test_budget_prunes_pricey_models_and_flags_degraded():
    # A budget that only affords the cheapest models excludes the expensive ones.
    c = select_model(
        tier="frontier", difficulty="HARD", policy="best",
        remaining_budget_usd=0.02, input_tokens=1000, max_tokens=1000,
    )
    assert c.degraded is True
    # whatever was chosen must have been affordable (cheaper than opus, which is ~$0.09)
    assert c.model_id != "us.anthropic.claude-opus-4-1-20250805-v1:0"


def test_zero_budget_degrades_to_cheapest_never_raises():
    c = select_model(
        tier="frontier", difficulty="HARD", policy="best",
        remaining_budget_usd=0.0, input_tokens=1000, max_tokens=1000,
    )
    assert c.model_id == models_for_tier("frontier")[0]  # cheapest entitled
    assert c.degraded is True


def test_unbounded_budget_is_all_affordable():
    c = select_model(tier="oss", difficulty="HARD", policy="best", remaining_budget_usd=None)
    assert c.degraded is False


def test_resolve_model_pin_wins_only_within_entitlement():
    oss = models_for_tier("oss")
    # a valid in-tier pin wins
    assert resolve_model(oss[0], oss[1], oss) == oss[1]
    # a frontier pin from an oss session is DROPPED (fail-closed), routed kept
    frontier_id = "us.anthropic.claude-opus-4-1-20250805-v1:0"
    assert resolve_model(oss[0], frontier_id, oss) == oss[0]
    # no pin keeps routed
    assert resolve_model(oss[0], None, oss) == oss[0]


# --- run_model_router orchestration -----------------------------------------


class FakeDifficultyBackend:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def converse(self, tier, system, prompt, max_tokens):
        self.calls += 1
        assert system == DIFFICULTY_SYSTEM  # it's the difficulty classifier, not the mode one
        return self.reply, {"inputTokens": 10, "outputTokens": 1}, None


def test_run_model_router_classifies_then_selects_and_emits():
    events, emit = _collect()
    backend = FakeDifficultyBackend("SIMPLE")
    meter = FakeMeter()
    choice = run_model_router(
        backend=backend, meter=meter, emit=emit, question="what is X", router=ROUTER,
        tier="frontier", policy="thrifty",
    )
    assert isinstance(choice, ModelChoice)
    assert backend.calls == 1
    assert choice.model_id == models_for_tier("frontier")[0]  # SIMPLE thrifty -> cheapest
    assert "difficulty" in meter.labels
    assert any(e["type"] == "model_route" and e["model"] == choice.model_id for e in events)


def test_run_model_router_pin_short_circuits_no_call_no_spend():
    events, emit = _collect()
    backend = FakeDifficultyBackend("HARD")  # would classify HARD if called
    meter = FakeMeter()
    pinned = models_for_tier("oss")[1]
    choice = run_model_router(
        backend=backend, meter=meter, emit=emit, question="x", router=ROUTER,
        tier="oss", pin=pinned,
    )
    assert choice.model_id == pinned
    assert backend.calls == 0  # no classifier call
    assert meter.total == 0.0  # no spend
    assert [e for e in events if e["type"] == "model_route"][0]["pinned"] is True


def test_run_model_router_drops_unentitled_pin_and_routes():
    events, emit = _collect()
    backend = FakeDifficultyBackend("SIMPLE")
    choice = run_model_router(
        backend=backend, meter=FakeMeter(), emit=emit, question="x", router=ROUTER,
        tier="oss", pin="us.anthropic.claude-opus-4-1-20250805-v1:0",  # not entitled
    )
    assert backend.calls == 1  # unentitled pin dropped -> classifier runs
    assert choice.model_id in set(models_for_tier("oss"))  # stays in tier
