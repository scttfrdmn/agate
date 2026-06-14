"""Shared orchestration contracts (Protocols + type aliases).

The Panel, Analyze, and router orchestrations all drive the same injected surfaces
— a model `Backend`, a `CostMeter`, and an `Emit` sink. They were each redefining
these; defining them once here keeps the contract single-sourced. The deployed
agent supplies Bedrock-backed implementations; tests supply fakes.

Pure typing — no AWS, no runtime cost.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

# A usage dict as returned in a Converse response.
Usage = dict[str, int]

# The event sink the orchestrations emit run events to (transport-agnostic).
Emit = Callable[[dict[str, Any]], None]


class Backend(Protocol):
    """Minimal model-invocation surface (mirrors the chat path)."""

    def converse(
        self, tier: str, system: str, prompt: str, max_tokens: int
    ) -> tuple[str, Usage, Any]:
        """Return (text, usage, matches); `matches` is retrieval metadata."""
        ...


class CostMeter(Protocol):
    """Thread-safe running cost meter. `add_compute` is used only by Analyze, but
    the canonical `cost.CostMeter` implements all of these, so one Protocol serves
    every orchestration."""

    @property
    def total(self) -> float: ...

    def add_llm(self, label: str, tier: str, model_label: str, usage: Usage) -> float: ...

    def add_compute(self, label: str, seconds: float) -> float: ...
