"""Mode router (§10.2.2) — a cheap routing call picks a default interaction mode.

A single fast, tiny (`max_tokens≈5`) model call classifies free-form input to one
word, which maps to a mode: SYNTHESIS→Ask, DEBATE→Panel, ANALYSIS→Analyze. The user
can always override and force a mode (academics prefer explicit control), and the
override wins. The routing call is metered (it appears on the receipt) but is NOT
rendered as an answer step — it emits only a `route` event.

Pure classification + precedence logic, AWS-free; `run_router` orchestrates the
cheap call over an injected `Backend`/`CostMeter` (fakes in tests).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

Mode = Literal["SYNTHESIS", "DEBATE", "ANALYSIS"]
MODES: tuple[Mode, ...] = ("SYNTHESIS", "DEBATE", "ANALYSIS")

# The cheapest-mode default: a single cited synthesis (Ask, Tier 0). When the
# router is ambiguous we fall back here — never to a more expensive mode.
DEFAULT_MODE: Mode = "SYNTHESIS"

Emit = Callable[[dict[str, Any]], None]
Usage = dict[str, int]

# The routing system prompt: classify to exactly one word from the vocabulary.
ROUTER_SYSTEM = """\
Classify the user's request into exactly one of these modes, replying with ONLY the
single word, in upper case, and nothing else:

- SYNTHESIS : the user wants a direct, cited answer drawn from the corpus.
- DEBATE    : the user wants multiple models to weigh in and have disagreements surfaced.
- ANALYSIS  : the user wants data computed, code written/run, or a chart produced.

Reply with one word: SYNTHESIS, DEBATE, or ANALYSIS.
"""

# Cue words that disambiguate a noisy classification toward a mode. Order matters:
# checked only when the model's word isn't itself a clean mode token.
_CUES: dict[Mode, tuple[str, ...]] = {
    "ANALYSIS": ("analy", "compute", "calculat", "plot", "chart", "code", "graph"),
    "DEBATE": ("debate", "panel", "compare", "disagree", "perspective", "contrast"),
    "SYNTHESIS": ("synth", "ask", "summar", "explain", "cite", "answer"),
}


class Backend(Protocol):
    def converse(
        self, tier: str, system: str, prompt: str, max_tokens: int
    ) -> tuple[str, Usage, Any]: ...


class CostMeter(Protocol):
    @property
    def total(self) -> float: ...

    def add_llm(self, label: str, tier: str, model_label: str, usage: Usage) -> float: ...


def classify_mode(raw: str) -> Mode:
    """Map a router model's (possibly noisy) one-word reply to a Mode.

    Robust to surrounding punctuation/casing/extra words. An exact mode token wins;
    otherwise the first matching cue word decides; failing all, the cheapest default
    (SYNTHESIS) — the router never escalates to a costlier mode on ambiguity.
    """
    text = raw.strip().upper()
    # Exact token anywhere in the reply (handles "Mode: DEBATE." etc.).
    for mode in MODES:
        if mode in text:
            return mode
    lowered = raw.lower()
    for mode, cues in _CUES.items():
        if any(cue in lowered for cue in cues):
            return mode
    return DEFAULT_MODE


def resolve_mode(routed: Mode, override: str | None) -> Mode:
    """Explicit user override wins over the routed default (§10.2.2).

    An override that isn't a valid mode is ignored (we keep the routed default
    rather than fail) — the UI constrains choices, but be defensive.
    """
    if override:
        candidate = override.strip().upper()
        if candidate in MODES:
            return candidate  # type: ignore[return-value]
    return routed


def run_router(
    *,
    backend: Backend,
    meter: CostMeter,
    emit: Emit,
    question: str,
    router: dict[str, Any],
    override: str | None = None,
) -> Mode:
    """Resolve the interaction mode for a free-form request.

    If `override` is a valid mode, short-circuit: no routing call, no spend — the
    user already chose. Otherwise make the cheap routing call (tiny max_tokens),
    meter it, and emit a `route` event (never an `answer`). Returns the chosen mode.
    """
    if override and override.strip().upper() in MODES:
        forced = resolve_mode(DEFAULT_MODE, override)
        emit({"type": "route", "mode": forced})
        return forced

    # Tiny, fast classification call (max_tokens≈5).
    max_tok = int(router.get("max_tokens", 5))
    raw, usage, _ = backend.converse(router["tier"], ROUTER_SYSTEM, question, max_tok)
    meter.add_llm("router", router["tier"], router.get("label", "router"), usage)
    emit({"type": "cost", "total": round(meter.total, 6)})

    mode = classify_mode(raw)
    emit({"type": "route", "mode": mode})
    return mode
