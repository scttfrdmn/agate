"""Unit tests for the adjudicator contract: Divergence model + strip_fences. No AWS."""

from __future__ import annotations

import json

import pytest
from agate.panel.schema import Divergence, strip_fences
from pydantic import ValidationError

# The §10.2.5 worked example, with neutral pane labels (repo convention).
WORKED_EXAMPLE = {
    "summary": "Agree on direction, differ on magnitude; one unsupported safety claim.",
    "claims": [
        {
            "id": "c1",
            "text": "Treatment lowers the marker.",
            "kind": "agreement",
            "positions": [
                {"pane": "frontier", "stance": "supports"},
                {"pane": "open-weight-70b", "stance": "supports"},
            ],
            "verify": False,
            "evidence_refs": ["DOC1", "DOC2"],
        },
        {
            "id": "c2",
            "text": "The effect is clinically large.",
            "kind": "disagreement",
            "positions": [
                {"pane": "frontier", "stance": "partial", "note": "trial-dependent"},
                {"pane": "open-weight-70b", "stance": "supports"},
            ],
            "verify": True,
            "evidence_refs": ["DOC2"],
        },
    ],
}


def test_validates_worked_example():
    div = Divergence.model_validate(WORKED_EXAMPLE)
    assert div.summary.startswith("Agree")
    assert len(div.claims) == 2
    assert div.claims[1].kind == "disagreement"
    assert div.claims[1].positions[0].note == "trial-dependent"


def test_evidence_refs_default_empty():
    div = Divergence.model_validate(
        {
            "summary": "s",
            "claims": [
                {
                    "id": "c1",
                    "text": "t",
                    "kind": "unsupported",
                    "positions": [{"pane": "frontier", "stance": "disputes"}],
                    "verify": True,
                }
            ],
        }
    )
    assert div.claims[0].evidence_refs == []


def test_rejects_unknown_stance():
    with pytest.raises(ValidationError):
        Divergence.model_validate(
            {
                "summary": "s",
                "claims": [
                    {
                        "id": "c1",
                        "text": "t",
                        "kind": "agreement",
                        "positions": [{"pane": "frontier", "stance": "maybe"}],
                        "verify": False,
                    }
                ],
            }
        )


def test_rejects_unknown_kind():
    bad = json.loads(json.dumps(WORKED_EXAMPLE))
    bad["claims"][0]["kind"] = "mostly-agree"
    with pytest.raises(ValidationError):
        Divergence.model_validate(bad)


def test_rejects_additional_properties():
    bad = json.loads(json.dumps(WORKED_EXAMPLE))
    bad["claims"][0]["surprise"] = "field"
    with pytest.raises(ValidationError):
        Divergence.model_validate(bad)


def test_rejects_empty_positions():
    with pytest.raises(ValidationError):
        Divergence.model_validate(
            {
                "summary": "s",
                "claims": [
                    {"id": "c1", "text": "t", "kind": "agreement", "positions": [], "verify": False}
                ],
            }
        )


def test_missing_required_field_rejected():
    with pytest.raises(ValidationError):
        Divergence.model_validate({"claims": []})  # no summary


@pytest.mark.parametrize(
    "raw,expected_key",
    [
        ('{"summary": "x", "claims": []}', "summary"),
        ('```json\n{"summary": "x", "claims": []}\n```', "summary"),
        ('```\n{"summary": "x", "claims": []}\n```', "summary"),
        ('   {"summary": "x", "claims": []}   ', "summary"),
    ],
)
def test_strip_fences_yields_parseable_json(raw, expected_key):
    payload = json.loads(strip_fences(raw))
    assert expected_key in payload


def test_strip_fences_leaves_plain_text_alone():
    assert strip_fences("not json at all") == "not json at all"
