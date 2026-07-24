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

from dataclasses import dataclass
from typing import Any, Literal

from cost.precall import estimate_call_cost

from agate.contracts import Backend, CostMeter, Emit
from agate.entitlements import Tier, models_for_tier, tier_for_model

Mode = Literal["SYNTHESIS", "DEBATE", "ANALYSIS"]
MODES: tuple[Mode, ...] = ("SYNTHESIS", "DEBATE", "ANALYSIS")

# The cheapest-mode default: a single cited synthesis (Ask, Tier 0). When the
# router is ambiguous we fall back here — never to a more expensive mode.
DEFAULT_MODE: Mode = "SYNTHESIS"

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


# --- model axis (#122): entitlement-and-budget-aware "auto mode" ------------
# The Claude-Code-like surface: state intent, the system routes — but the candidate
# set IS the entitled set (`models_for_tier`) and every pick is budget-pre-checked, so
# auto mode can NEVER select a model above the session's tier or beyond its budget.
# Mirrors the mode axis above (cheapest-default, classifier edge, pin-wins precedence).

Difficulty = Literal["SIMPLE", "MODERATE", "HARD"]
DIFFICULTIES: tuple[Difficulty, ...] = ("SIMPLE", "MODERATE", "HARD")
# Cheapest default on ambiguity — the router never escalates to a costlier model.
DEFAULT_DIFFICULTY: Difficulty = "SIMPLE"

RoutingPolicy = Literal["thrifty", "best"]
DEFAULT_POLICY: RoutingPolicy = "thrifty"

DIFFICULTY_SYSTEM = """\
Rate how much model capability the user's request needs, replying with ONLY the single
word, in upper case, and nothing else:

- SIMPLE   : a short factual answer, lookup, or summary a small model handles well.
- MODERATE : multi-step reasoning, careful synthesis, or moderate code.
- HARD     : deep reasoning, hard analysis, or work that needs the most capable model.

Reply with one word: SIMPLE, MODERATE, or HARD.
"""

_DIFFICULTY_CUES: dict[Difficulty, tuple[str, ...]] = {
    "HARD": ("prove", "derive", "rigorous", "complex", "optimi", "deep", "hard"),
    "MODERATE": ("explain", "compare", "analy", "reason", "step", "design"),
    "SIMPLE": ("what is", "define", "list", "summar", "when", "who"),
}


def classify_difficulty(raw: str) -> Difficulty:
    """Map a classifier model's one-word reply to a Difficulty. Exact token wins; then
    a cue word; failing all, the cheapest default (SIMPLE) — never escalates on noise.
    Mirrors `classify_mode`."""
    text = raw.strip().upper()
    for d in DIFFICULTIES:
        if d in text:
            return d
    lowered = raw.lower()
    for d, cues in _DIFFICULTY_CUES.items():
        if any(cue in lowered for cue in cues):
            return d
    return DEFAULT_DIFFICULTY


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """The routed model + the human-readable why. `degraded` is True when budget forced
    a cheaper pick than the policy/difficulty would otherwise choose."""

    model_id: str
    reason: str
    policy: RoutingPolicy
    difficulty: Difficulty
    degraded: bool = False


def _affordable(
    candidates: list[str], remaining: float | None, input_tokens: int, max_tokens: int
) -> list[str]:
    """Subset of `candidates` whose worst-case call cost fits `remaining` budget.
    `remaining is None` → unbounded (all affordable). Budget clamp runs BEFORE policy so
    an unaffordable model is never selected (fail-closed)."""
    if remaining is None:
        return list(candidates)
    out = []
    for m in candidates:
        cost = estimate_call_cost(m, input_tokens, max_tokens, fallback_tier=tier_for_model(m))
        if cost <= remaining:
            out.append(m)
    return out


def select_model(
    *,
    tier: Tier,
    difficulty: Difficulty = DEFAULT_DIFFICULTY,
    policy: RoutingPolicy = DEFAULT_POLICY,
    remaining_budget_usd: float | None = None,
    input_tokens: int = 0,
    max_tokens: int = 1024,
    pricebook=None,  # noqa: ANN001 — optional cost.PriceBook, kept loose to avoid the import here
) -> ModelChoice:
    """Pick a model for `tier`, bounded by entitlement AND budget — the heart of #122.

    The candidate set IS `models_for_tier(tier)` (cheapest-first), so the result can
    NEVER exceed the session's entitled tier. Candidates are filtered to those whose
    worst-case cost fits the remaining budget, THEN the policy chooses within that
    affordable set: `thrifty` picks the cheapest that clears the difficulty bar; `best`
    picks the most capable affordable. If nothing is affordable, degrade to the cheapest
    entitled model (never raise, never exceed)."""
    candidates = models_for_tier(tier)  # cheapest-first, entitled set
    affordable = _affordable(candidates, remaining_budget_usd, input_tokens, max_tokens)
    if not affordable:
        # Budget can't afford even the cheapest entitled model: pick it anyway (the
        # chokepoint makes the real pre-call decision) and flag the degradation.
        return ModelChoice(
            model_id=candidates[0],
            reason="budget below the cheapest entitled model — using it (gate decides)",
            policy=policy,
            difficulty=difficulty,
            degraded=True,
        )
    degraded = len(affordable) < len(candidates)  # budget pruned some pricier options
    if policy == "best":
        chosen = affordable[-1]  # most capable affordable
        why = "best: most capable model the budget affords"
    else:  # thrifty: cheapest that clears the difficulty bar
        idx = {"SIMPLE": 0, "MODERATE": len(affordable) // 2, "HARD": len(affordable) - 1}[
            difficulty
        ]
        chosen = affordable[idx]
        match = "cheapest" if idx == 0 else "capability-matched"
        why = f"thrifty: {difficulty} task → {match} affordable model"
    if degraded:
        why += " (budget pruned costlier options)"
    return ModelChoice(
        model_id=chosen, reason=why, policy=policy, difficulty=difficulty, degraded=degraded
    )


def resolve_model(routed: str, pin: str | None, entitled: list[str]) -> str:
    """Explicit user pin wins — but ONLY if it's in the entitled set (fail-closed: a pin
    outside entitlement is dropped, never escalates). Mirrors `resolve_mode`."""
    if pin and pin in entitled:
        return pin
    return routed


def run_model_router(
    *,
    backend: Backend,
    meter: CostMeter,
    emit: Emit,
    question: str,
    router: dict[str, Any],
    tier: Tier,
    policy: RoutingPolicy = DEFAULT_POLICY,
    pin: str | None = None,
    remaining_budget_usd: float | None = None,
    input_tokens: int = 0,
    max_tokens: int = 1024,
) -> ModelChoice:
    """Resolve which model handles a request. A valid `pin` (in the entitled set)
    short-circuits — no classifier call, no spend. Otherwise the tiny difficulty
    classifier runs (metered), then `select_model`. Emits a `model_route` event with the
    chosen model + the human why. Mirrors `run_router`."""
    entitled = models_for_tier(tier)
    if pin and pin in entitled:
        emit({"type": "model_route", "model": pin, "reason": "pinned by the user", "pinned": True})
        return ModelChoice(
            model_id=pin,
            reason="pinned by the user",
            policy=policy,
            difficulty=DEFAULT_DIFFICULTY,
            degraded=False,
        )

    max_tok = int(router.get("max_tokens", 5))
    raw, usage, _ = backend.converse(router["tier"], DIFFICULTY_SYSTEM, question, max_tok)
    meter.add_llm("difficulty", router["tier"], router.get("label", "difficulty"), usage)
    emit({"type": "cost", "total": round(meter.total, 6)})

    choice = select_model(
        tier=tier,
        difficulty=classify_difficulty(raw),
        policy=policy,
        remaining_budget_usd=remaining_budget_usd,
        input_tokens=input_tokens,
        max_tokens=max_tokens,
    )
    emit(
        {
            "type": "model_route",
            "model": choice.model_id,
            "reason": choice.reason,
            "degraded": choice.degraded,
        }
    )
    return choice
