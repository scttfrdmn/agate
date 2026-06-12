"""Unit tests for the reproducible run artifact (§10.2.8). No AWS."""

from __future__ import annotations

import json

import pytest
from agg.artifact import RunArtifact, build_artifact, receipt_to_csv, to_json
from pydantic import ValidationError

CREATED = "2026-06-11T12:00:00Z"
ROSTER = [
    {"tier": "frontier", "label": "frontier", "max_tokens": 512},
    {"tier": "open-weight-70b", "label": "open-weight-70b", "max_tokens": 512},
]

# A realistic Panel run event stream (mirrors run_panel output).
PANEL_EVENTS = [
    {"type": "route", "mode": "DEBATE"},
    {
        "type": "model",
        "tier": "frontier",
        "label": "frontier",
        "state": "start",
        "pane": "frontier",
    },
    {
        "type": "model",
        "tier": "open-weight-70b",
        "label": "open-weight-70b",
        "state": "start",
        "pane": "open-weight-70b",
    },
    {"type": "answer", "pane": "frontier", "text": "LDL-C drops."},
    {"type": "answer", "pane": "open-weight-70b", "text": "LDL-C falls."},
    {
        "type": "citation",
        "source": "PMC4521",
        "modality": "image",
        "ref": "figure-3",
        "thumb": "QUJD",
    },
    {"type": "citation", "source": "DOC1", "modality": "text", "ref": "p2"},
    {
        "type": "divergence",
        "summary": "Agree on direction, differ on magnitude.",
        "claims": [
            {
                "id": "c1",
                "text": "Treatment lowers the marker.",
                "kind": "agreement",
                "positions": [{"pane": "frontier", "stance": "supports"}],
                "verify": False,
            }
        ],
    },
    {"type": "answer", "title": "Panel — reconciled", "text": "Agree on direction."},
    {
        "type": "receipt",
        "rows": [
            {"label": "panel · frontier", "kind": "llm", "cost": 0.0012},
            {"label": "panel · open-weight-70b", "kind": "llm", "cost": 0.0003},
            {"label": "panel · adjudication", "kind": "llm", "cost": 0.0009},
        ],
        "total": 0.0024,
    },
]


def _build(**kw):
    return build_artifact(
        PANEL_EVENTS,
        run_id="run-1",
        created_at=CREATED,
        question="Does it work?",
        roster=ROSTER,
        **kw,
    )


def test_artifact_captures_transcript_and_models():
    art = _build()
    assert art.mode == "DEBATE"
    assert art.question == "Does it work?"
    assert art.models == ["frontier", "open-weight-70b"]  # first-seen order, deduped
    # transcript holds all answer turns (per-pane + the reconciled one)
    assert len(art.transcript) == 3
    assert art.transcript[-1].title == "Panel — reconciled"


def test_artifact_captures_citations_text_and_visual():
    art = _build()
    assert len(art.citations) == 2
    visual = next(c for c in art.citations if c.modality == "image")
    assert visual.ref == "figure-3"
    assert visual.thumb == "QUJD"


def test_artifact_captures_divergence_via_shared_model():
    art = _build()
    assert art.divergence is not None
    assert art.divergence.claims[0].kind == "agreement"


def test_artifact_captures_receipt_and_total():
    art = _build()
    assert len(art.receipt) == 3
    assert art.cost_total == pytest.approx(0.0024)


def test_artifact_carries_roster_for_reproducibility():
    art = _build()
    assert art.roster == ROSTER


def test_analyze_run_captures_code_cells():
    events = [
        {"type": "route", "mode": "ANALYSIS"},
        {"type": "code", "language": "python", "source": "print(6*7)"},
        {"type": "answer", "text": "42"},
    ]
    art = build_artifact(events, run_id="r2", created_at=CREATED)
    assert art.mode == "ANALYSIS"
    assert len(art.code) == 1
    assert art.code[0].source == "print(6*7)"


def test_cost_tag_flows_into_csv_export():
    art = _build(cost_tag="GRANT-NIH-123")
    csv_text = receipt_to_csv(art)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "run_id,cost_tag,label,kind,cost"
    assert all("GRANT-NIH-123" in line for line in lines[1:])
    # last row is the TOTAL
    assert lines[-1].split(",")[2] == "TOTAL"
    assert lines[-1].endswith("0.002400")


def test_csv_export_without_tag_uses_empty_tag():
    art = _build()
    csv_text = receipt_to_csv(art)
    # cost_tag column present but empty
    assert ",," in csv_text.splitlines()[1] or csv_text.splitlines()[1].split(",")[1] == ""


def test_to_json_roundtrips_through_the_model():
    art = _build(cost_tag="COURSE-CHEM101")
    blob = to_json(art)
    reloaded = RunArtifact.model_validate_json(blob)
    assert reloaded.run_id == "run-1"
    assert reloaded.cost_tag == "COURSE-CHEM101"
    assert reloaded.divergence.summary.startswith("Agree")
    assert reloaded.models == ["frontier", "open-weight-70b"]


def test_json_is_canonical_and_omits_none():
    art = build_artifact([{"type": "answer", "text": "hi"}], run_id="r3", created_at=CREATED)
    data = json.loads(to_json(art))
    # exclude_none drops unset optionals like question/divergence/cost_tag
    assert "question" not in data
    assert "divergence" not in data
    assert data["run_id"] == "r3"


def test_empty_stream_yields_minimal_artifact():
    art = build_artifact([], run_id="r0", created_at=CREATED)
    assert art.transcript == []
    assert art.models == []
    assert art.cost_total == 0.0


def test_artifact_rejects_unknown_top_level_field():
    with pytest.raises(ValidationError):
        RunArtifact.model_validate({"run_id": "x", "created_at": CREATED, "surprise": 1})
