"""Panel orchestration (§10.2.4–10.2.5) — N models read the same evidence
independently, then a structured adjudicator reconciles them.

Pure-Python and AWS-free: `run_panel` orchestrates over injected `Backend` and
`CostMeter` interfaces (the deployed agent supplies Bedrock-backed implementations;
tests supply fakes). The adjudicator's output is validated against the `Divergence`
Pydantic model before a `divergence` event is emitted; a malformed adjudication
falls back to an unstructured `answer` rather than failing the run.
"""

from agg.panel.orchestrator import Backend, CostMeter, run_panel
from agg.panel.prompts import ADJUDICATE_SYSTEM
from agg.panel.schema import Divergence, strip_fences

__all__ = [
    "ADJUDICATE_SYSTEM",
    "Backend",
    "CostMeter",
    "Divergence",
    "run_panel",
    "strip_fences",
]
