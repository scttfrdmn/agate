"""run_analyze — Analyze-mode orchestration (§10.2.6).

The model writes Python; an isolated microVM runs it; the code and its result are
both shown. Flow:
  1. (unless re-running) generate a script from the question -> emit a `code` event
     so the SPA renders an editable, re-runnable cell;
  2. execute it in the Code Interpreter microVM (injected `CodeRunner`);
  3. map the result to `answer`/`chart` events;
  4. add a `compute` line to the receipt (distinct from token cost) and emit `cost`.

Pure orchestration over injected interfaces — no boto3 here. The deployed agent
supplies a Bedrock `Backend`, an AgentCore Code Interpreter `CodeRunner`, and the
shared `CostMeter`; tests supply fakes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from agg.analyze.schema import ExecResult, extract_code, result_to_events

Emit = Callable[[dict[str, Any]], None]
Usage = dict[str, int]


class Backend(Protocol):
    """Model-invocation surface used to generate the script (same as elsewhere)."""

    def converse(
        self, tier: str, system: str, prompt: str, max_tokens: int
    ) -> tuple[str, Usage, Any]: ...


class CodeRunner(Protocol):
    """The isolated execution surface (AgentCore Code Interpreter microVM)."""

    def execute(self, code: str, *, language: str = "python") -> ExecResult:
        """Run code in the sandbox and return a normalised ExecResult."""
        ...


class CostMeter(Protocol):
    """Running cost meter with both token and compute lines."""

    @property
    def total(self) -> float: ...

    def add_llm(self, label: str, tier: str, model_label: str, usage: Usage) -> float: ...

    def add_compute(self, label: str, seconds: float) -> float:
        """Record a compute (execution-time) cost line and return its dollar amount."""
        ...


def run_analyze(
    *,
    backend: Backend | None,
    runner: CodeRunner,
    meter: CostMeter,
    emit: Emit,
    question: str,
    analyze_system: str,
    generator: dict[str, Any] | None = None,
    code: str | None = None,
) -> ExecResult:
    """Generate (or accept) a script, run it in the microVM, and emit events.

    Pass `code` to re-run an edited cell (skips generation — the user's edits are
    authoritative). Otherwise `backend` + `generator` ({"tier","label","max_tokens"})
    generate it from `question`. Returns the ExecResult for callers that want it.
    """
    if code is None:
        if backend is None or generator is None:
            raise ValueError("run_analyze needs either `code` (re-run) or backend+generator")
        raw, usage, _ = backend.converse(
            generator["tier"], analyze_system, question, generator["max_tokens"]
        )
        meter.add_llm("analyze · codegen", generator["tier"], generator["label"], usage)
        emit({"type": "cost", "total": round(meter.total, 6)})
        code = extract_code(raw)

    # Show the (editable, re-runnable) cell before executing it.
    emit({"type": "code", "language": "python", "source": code})

    result = runner.execute(code, language="python")

    # Compute metering — a distinct line from token spend (§10.2.6).
    meter.add_compute("analyze · execution", result.elapsed_s)
    emit({"type": "cost", "total": round(meter.total, 6)})

    for ev in result_to_events(result):
        emit(ev)

    return result
