"""The adjudicator contract (§10.2.5): the `Divergence` model + `strip_fences`.

This Pydantic model mirrors the draft-07 JSON schema in §10.2.5 exactly — it is
both the adjudicator's required output shape and the `divergence` event payload.
`run_panel` validates the adjudicator's raw text against this model before emitting;
on failure it falls back to an unstructured answer (never a hard mid-run failure).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

Stance = Literal["supports", "disputes", "partial", "silent"]
ClaimKind = Literal["agreement", "disagreement", "unsupported"]


class Position(BaseModel):
    """Where one reviewer stands on a claim. `pane` must equal a roster label."""

    model_config = ConfigDict(extra="forbid")  # additionalProperties: false

    pane: str
    stance: Stance
    note: str | None = None


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    kind: ClaimKind
    # minItems: 1 — every claim records at least one reviewer's position.
    positions: list[Position]
    verify: bool
    evidence_refs: list[str] = []

    def model_post_init(self, _context: object) -> None:
        if not self.positions:
            raise ValueError("claim must have at least one position")


class Divergence(BaseModel):
    """The full reconciliation: a summary plus discrete, classified claims."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    claims: list[Claim]


# Matches a leading ```json / ``` fence and the trailing fence, tolerating
# surrounding whitespace — adjudicators occasionally wrap JSON in Markdown despite
# the ONLY-JSON instruction.
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def strip_fences(raw: str) -> str:
    """Remove accidental Markdown code fences before `json.loads`.

    Strips a leading ```` ```json ```` / ```` ``` ```` opener and a trailing
    ```` ``` ```` closer. If no fences are present the text is returned trimmed.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Drop the opening fence line, then any trailing fence.
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()
