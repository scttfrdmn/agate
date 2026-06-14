"""Pure Analyze helpers — code extraction and result→event mapping (§10.2.6).

AWS-free and side-effect-free. `extract_code` pulls the Python script out of the
model's fenced output; `result_to_events` turns the microVM's content blocks
(text + image) into the `answer`/`chart` events the SPA renders. The Code
Interpreter result shape mirrors AgentCore's `InvokeCodeInterpreter` stream result:
a list of content blocks, each with a `type` and either `text` or base64 `data`
plus a `mimeType`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# A ```python ... ``` block (preferred), or any ``` ... ``` block as a fallback.
_PY_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_code(raw: str) -> str:
    """Extract the Python script from a model's response.

    Prefers a fenced block; if none is present, treats the whole (stripped) text as
    code — the Analyze prompt asks for a bare script, so an unfenced reply is still
    runnable. Returns the first fenced block when several are present.
    """
    match = _PY_FENCE.search(raw)
    if match:
        return match.group(1).strip()
    return raw.strip()


@dataclass(frozen=True, slots=True)
class ContentBlock:
    """One block of a Code Interpreter result (text or an inline image)."""

    type: str  # "text" | "image" | "resource" | ...
    text: str | None = None
    data: str | None = None  # base64 for images
    mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class ExecResult:
    """A normalised Code Interpreter execution result."""

    content: list[ContentBlock] = field(default_factory=list)
    is_error: bool = False
    # Wall-clock seconds the microVM spent — drives the compute metering line.
    elapsed_s: float = 0.0

    @property
    def stdout(self) -> str:
        """Concatenated text output across text blocks."""
        return "\n".join(b.text for b in self.content if b.type == "text" and b.text)

    @property
    def images(self) -> list[ContentBlock]:
        return [b for b in self.content if b.type == "image" and b.data]


def parse_invoke_result(raw: dict[str, Any], *, elapsed_s: float = 0.0) -> ExecResult:
    """Normalise an AgentCore `InvokeCodeInterpreter` stream result dict.

    Tolerant of the wire shape: `{"stream": {"result": {...}}}` or a bare result.
    Maps each MCP-style content block to a `ContentBlock`.
    """
    result = raw
    if "stream" in raw and isinstance(raw["stream"], dict):
        result = raw["stream"].get("result", {})
    elif "result" in raw and isinstance(raw["result"], dict):
        result = raw["result"]

    blocks: list[ContentBlock] = []
    for item in result.get("content", []) or []:
        btype = item.get("type", "text")
        blocks.append(
            ContentBlock(
                type=btype,
                text=item.get("text"),
                data=item.get("data"),
                mime_type=item.get("mimeType"),
            )
        )
    return ExecResult(
        content=blocks, is_error=bool(result.get("isError", False)), elapsed_s=elapsed_s
    )


def result_to_events(result: ExecResult, *, pane: str | None = None) -> list[dict[str, Any]]:
    """Map an execution result to `answer`/`chart` events (the SPA render contract).

    Text output becomes an `answer`; each image becomes a `chart`. An error result
    still surfaces its text (so the user sees the traceback) but is titled as a
    failure rather than swallowed.
    """
    events: list[dict[str, Any]] = []
    text = result.stdout.strip()
    if text:
        ev: dict[str, Any] = {"type": "answer", "text": text}
        if result.is_error:
            ev["title"] = "Analyze — error"
        if pane:
            ev["pane"] = pane
        events.append(ev)
    for img in result.images:
        chart: dict[str, Any] = {
            "type": "chart",
            "mime": img.mime_type or "image/png",
            "data": img.data,
        }
        if pane:
            chart["pane"] = pane
        events.append(chart)
    return events
