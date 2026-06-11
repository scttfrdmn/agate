"""Unit tests for run_analyze — fakes only, no AWS (§10.2.12 #3).

Covers: generate→code event→execute→chart/answer events; the re-run path (edited
code skips generation); compute metering as a line distinct from token cost; and
the error path surfacing a traceback.
"""

from __future__ import annotations

import pytest
from agg.analyze.orchestrator import run_analyze
from agg.analyze.prompts import ANALYZE_SYSTEM
from agg.analyze.schema import ContentBlock, ExecResult

GENERATOR = {"tier": "frontier", "label": "frontier", "max_tokens": 1024}


class FakeBackend:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def converse(self, tier, system, prompt, max_tokens):
        self.calls += 1
        return self.reply, {"inputTokens": 50, "outputTokens": 30}, None


class FakeRunner:
    def __init__(self, result: ExecResult):
        self.result = result
        self.executed: list[str] = []

    def execute(self, code, *, language="python"):
        self.executed.append(code)
        return self.result


class FakeMeter:
    def __init__(self):
        self._total = 0.0
        self.lines: list[tuple[str, str]] = []  # (kind, label)

    @property
    def total(self):
        return self._total

    def add_llm(self, label, tier, model_label, usage):
        self._total += 0.002
        self.lines.append(("llm", label))
        return 0.002

    def add_compute(self, label, seconds):
        inc = 0.0001 * seconds
        self._total += inc
        self.lines.append(("compute", label))
        return inc


def _collect():
    events: list[dict] = []
    return events, events.append


def test_generate_then_execute_emits_code_then_chart():
    events, emit = _collect()
    backend = FakeBackend("```python\nprint('mean=2')\n```")
    runner = FakeRunner(
        ExecResult(
            content=[
                ContentBlock(type="text", text="mean=2"),
                ContentBlock(type="image", data="QUJD", mime_type="image/png"),
            ],
            elapsed_s=2.0,
        )
    )
    meter = FakeMeter()
    run_analyze(
        backend=backend,
        runner=runner,
        meter=meter,
        emit=emit,
        question="What is the mean of [1,2,3]?",
        analyze_system=ANALYZE_SYSTEM,
        generator=GENERATOR,
    )

    types = [e["type"] for e in events]
    # code event precedes execution output; chart follows the answer
    assert types.index("code") < types.index("chart")
    code_ev = next(e for e in events if e["type"] == "code")
    assert code_ev["source"] == "print('mean=2')"  # extracted from the fence
    assert any(e["type"] == "answer" and e["text"] == "mean=2" for e in events)
    assert any(e["type"] == "chart" and e["data"] == "QUJD" for e in events)
    assert backend.calls == 1
    assert runner.executed == ["print('mean=2')"]


def test_compute_metering_is_distinct_from_tokens():
    events, emit = _collect()
    meter = FakeMeter()
    run_analyze(
        backend=FakeBackend("```python\nprint(1)\n```"),
        runner=FakeRunner(ExecResult(content=[ContentBlock(type="text", text="1")], elapsed_s=3.0)),
        meter=meter,
        emit=emit,
        question="q",
        analyze_system=ANALYZE_SYSTEM,
        generator=GENERATOR,
    )
    kinds = [kind for kind, _ in meter.lines]
    assert "llm" in kinds and "compute" in kinds  # both lines present
    assert meter.lines[0][0] == "llm"  # codegen first
    assert meter.lines[-1][0] == "compute"  # execution after


def test_rerun_path_skips_generation():
    events, emit = _collect()
    backend = FakeBackend("should not be called")
    runner = FakeRunner(ExecResult(content=[ContentBlock(type="text", text="42")], elapsed_s=1.0))
    meter = FakeMeter()
    run_analyze(
        backend=backend,
        runner=runner,
        meter=meter,
        emit=emit,
        question="ignored on re-run",
        analyze_system=ANALYZE_SYSTEM,
        code="print(6*7)",  # user-edited cell
    )
    assert backend.calls == 0  # no generation on re-run
    assert runner.executed == ["print(6*7)"]
    # only a compute line, no llm line
    assert [k for k, _ in meter.lines] == ["compute"]
    code_ev = next(e for e in events if e["type"] == "code")
    assert code_ev["source"] == "print(6*7)"


def test_error_result_surfaces_traceback():
    events, emit = _collect()
    runner = FakeRunner(
        ExecResult(
            content=[ContentBlock(type="text", text="ZeroDivisionError")],
            is_error=True,
            elapsed_s=0.5,
        )
    )
    run_analyze(
        backend=None,
        runner=runner,
        meter=FakeMeter(),
        emit=emit,
        question="q",
        analyze_system=ANALYZE_SYSTEM,
        code="1/0",
    )
    err = next(e for e in events if e["type"] == "answer")
    assert err["title"] == "Analyze — error"
    assert "ZeroDivisionError" in err["text"]


def test_rerun_without_code_or_backend_is_an_error():
    with pytest.raises(ValueError):
        run_analyze(
            backend=None,
            runner=FakeRunner(ExecResult()),
            meter=FakeMeter(),
            emit=lambda e: None,
            question="q",
            analyze_system=ANALYZE_SYSTEM,
        )
