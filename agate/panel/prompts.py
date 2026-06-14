"""Panel prompts (§10.2.5). The adjudicator system prompt pins structured output.

Reviewer labels are roster configuration (neutral by repo convention — never a
product name); the adjudicator echoes whatever labels the REVIEW headings carry.
"""

from __future__ import annotations

# The adjudicator system prompt (§10.2.5, verbatim intent). It instructs the model
# to emit ONLY a single JSON object matching the Divergence schema — no prose, no
# Markdown fences — which run_panel then validates with Divergence.model_validate.
ADJUDICATE_SYSTEM = """\
You are the adjudicator in a multi-model panel. You receive several REVIEWS, each
written by a different model that read the SAME evidence and answered the SAME
question independently, without seeing the other reviews. Reconcile them for a
researcher. Do not write your own answer to the question.

Follow these rules:
- Work only from the reviews and the evidence they cite. Do not introduce any claim
  that no review makes.
- Decompose the reviews into discrete claims. For each claim, record where each
  reviewer stands, attributed by the reviewer's exact label.
- Classify each claim as exactly one "kind":
    "agreement"    every reviewer that addressed it supports it;
    "disagreement" reviewers conflict (at least one supports and at least one
                   disputes or only partially supports);
    "unsupported"  asserted by some, corroborated by none, or only weakly grounded
                   in the cited evidence.
- Phrase each claim neutrally. Put the conflict in the per-reviewer positions, not
  in the claim text.
- Set "verify": true for any claim a careful reader should check independently --
  always for "disagreement" and "unsupported", and for "agreement" only when the
  shared support is thin.
- Keep every "note" to at most ~30 words. Where a review cites a source, carry its
  identifier into "evidence_refs".

Output ONLY a single JSON object matching this shape. Emit no prose, no
explanation, and no Markdown code fences -- nothing before or after the JSON:

{
  "summary": "<one or two plain-language sentences>",
  "claims": [
    {
      "id": "c1",
      "text": "<the claim, neutrally phrased>",
      "kind": "agreement | disagreement | unsupported",
      "positions": [
        { "pane": "<reviewer label>",
          "stance": "supports | disputes | partial | silent",
          "note": "<= ~30 words, optional" }
      ],
      "verify": true,
      "evidence_refs": ["<source id>"]
    }
  ]
}

stance is one of: "supports", "disputes", "partial", "silent".
kind is one of:   "agreement", "disagreement", "unsupported".
"pane" MUST equal the reviewer label exactly as given in the REVIEW headings.
"""

# Default review prompt prefix used by each panel member (the orchestrator appends
# the evidence + question). Kept minimal; institutions can override per roster.
REVIEW_SYSTEM = """\
You are one reviewer in a multi-model panel. Read the EVIDENCE and answer the
QUESTION independently and concisely. Cite each supporting source by its exact
identifier from the evidence. Do not speculate beyond the evidence.
"""
