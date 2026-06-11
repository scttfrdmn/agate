"""Analyze mode (§10.2.6) — the model writes Python; an isolated microVM runs it.

The generated code is shown as an editable, re-runnable cell (`code` event); the
chart/numeric result renders inline (`chart`/`answer` events); execution time is
metered as a distinct compute line on the receipt (`add_compute`), separate from
token cost.

Pure-Python and AWS-free in core: `run_analyze` orchestrates over injected
`Backend` (code generation), `CodeRunner` (the Code Interpreter microVM), and
`CostMeter` interfaces. The deployed agent (AgentCore Runtime + Code Interpreter)
supplies real implementations; tests supply fakes.
"""

from agg.analyze.orchestrator import (
    Backend,
    CodeRunner,
    CostMeter,
    ExecResult,
    run_analyze,
)
from agg.analyze.prompts import ANALYZE_SYSTEM
from agg.analyze.schema import (
    ContentBlock,
    extract_code,
    parse_invoke_result,
    result_to_events,
)

__all__ = [
    "ANALYZE_SYSTEM",
    "Backend",
    "CodeRunner",
    "ContentBlock",
    "CostMeter",
    "ExecResult",
    "extract_code",
    "parse_invoke_result",
    "result_to_events",
    "run_analyze",
]
