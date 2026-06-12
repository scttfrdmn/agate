"""Reproducible run artifact (§10.2.8) — serialise a run into a citable record.

A run IS a typed event stream, so "save run" is a pure fold over that stream into a
single shareable, citable record: the transcript, the panel roster, the generated
code, every citation (text and visual), the divergence structure, the models used,
and the cost. Reproducibility is treated as a requirement: the artifact captures
everything needed to cite or re-run the work.

Pure and AWS-free. The deployed agent serialises server-side and stores the JSON
(then emits an `artifact` event with its URL); the browser's "save run" mirrors
this with the same shape. The `Divergence` model is reused from agg.panel so the
artifact's divergence field has one source of truth.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agg.panel.schema import Divergence

CitationModality = Literal["text", "image", "table", "audio", "video"]


class TranscriptTurn(BaseModel):
    """One answer turn in the run (per-pane for Panel, single for Ask)."""

    model_config = ConfigDict(extra="forbid")

    pane: str | None = None
    title: str | None = None
    text: str


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    modality: CitationModality
    ref: str
    thumb: str | None = None


class CodeCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str
    source: str


class ReceiptRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    kind: Literal["llm", "compute", "retrieval"]
    cost: float


class RunArtifact(BaseModel):
    """The serialised, reproducible record of a run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    created_at: str  # ISO-8601, supplied by the caller (no clock in core)
    mode: str | None = None  # SYNTHESIS | DEBATE | ANALYSIS
    question: str | None = None
    # The panel roster (mixed families/weights) — reproducibility input.
    roster: list[dict[str, Any]] = Field(default_factory=list)
    # Distinct model labels that actually produced output in this run.
    models: list[str] = Field(default_factory=list)
    transcript: list[TranscriptTurn] = Field(default_factory=list)
    code: list[CodeCell] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    divergence: Divergence | None = None
    receipt: list[ReceiptRow] = Field(default_factory=list)
    cost_total: float = 0.0
    # Optional chargeback tag (grant or course code) for the receipt export.
    cost_tag: str | None = None


def build_artifact(
    events: list[dict[str, Any]],
    *,
    run_id: str,
    created_at: str,
    question: str | None = None,
    roster: list[dict[str, Any]] | None = None,
    cost_tag: str | None = None,
) -> RunArtifact:
    """Fold an ordered run event stream into a RunArtifact.

    Reads the same event shapes the SPA renders (§10.2.9): `route`, `model`,
    `answer`, `code`, `citation`, `divergence`, `cost`, `receipt`. `created_at` is
    passed in (the core has no clock); `run_id` is the caller's identifier.
    """
    artifact = RunArtifact(
        run_id=run_id,
        created_at=created_at,
        question=question,
        roster=list(roster or []),
        cost_tag=cost_tag,
    )

    models_seen: list[str] = []
    for ev in events:
        etype = ev.get("type")
        if etype == "route":
            artifact.mode = ev.get("mode")
        elif etype == "model":
            label = ev.get("label")
            if label and label not in models_seen:
                models_seen.append(label)
        elif etype == "answer":
            artifact.transcript.append(
                TranscriptTurn(
                    pane=ev.get("pane"),
                    title=ev.get("title"),
                    text=ev.get("text", ""),
                )
            )
        elif etype == "code":
            artifact.code.append(
                CodeCell(language=ev.get("language", "python"), source=ev.get("source", ""))
            )
        elif etype == "citation":
            artifact.citations.append(
                Citation(
                    source=ev["source"],
                    modality=ev["modality"],
                    ref=ev["ref"],
                    thumb=ev.get("thumb"),
                )
            )
        elif etype == "divergence":
            # The event payload IS the Divergence shape (summary + claims).
            artifact.divergence = Divergence.model_validate(
                {"summary": ev.get("summary", ""), "claims": ev.get("claims", [])}
            )
        elif etype == "cost":
            artifact.cost_total = ev.get("total", artifact.cost_total)
        elif etype == "receipt":
            artifact.receipt = [ReceiptRow.model_validate(r) for r in ev.get("rows", [])]
            artifact.cost_total = ev.get("total", artifact.cost_total)

    artifact.models = models_seen
    return artifact


def receipt_to_csv(artifact: RunArtifact) -> str:
    """Render the artifact's receipt as CSV, tagged for chargeback (§10.2.8).

    Columns: run_id, cost_tag, label, kind, cost. A trailing TOTAL row carries the
    run total. Importable into a grant/course cost ledger.
    """
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["run_id", "cost_tag", "label", "kind", "cost"])
    tag = artifact.cost_tag or ""
    for row in artifact.receipt:
        writer.writerow([artifact.run_id, tag, row.label, row.kind, f"{row.cost:.6f}"])
    writer.writerow([artifact.run_id, tag, "TOTAL", "", f"{artifact.cost_total:.6f}"])
    return buf.getvalue()


def to_json(artifact: RunArtifact) -> str:
    """Serialise the artifact to canonical JSON (the shareable record)."""
    return artifact.model_dump_json(indent=2, exclude_none=True)
