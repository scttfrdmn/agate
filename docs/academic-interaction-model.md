# §10.2 — Academic Interaction Model

> **Status:** Design specification. Slots into `docs/aws-genai-gateway-design.md`
> immediately after §10.1 (visual / low-code authoring).
>
> **How to use this document (for Claude Code):** Treat this as the authoritative
> spec for the academic interaction model. Implement against it in the phases at
> the end. Honour `CLAUDE.md` in all cases — NO CLOCKS, GitHub-only project
> management (open Issues; do **not** add status/roadmap/TODO files to the repo),
> SemVer + Keep a Changelog, pinned toolchain. This document is design
> documentation and may live in `docs/`; the phase list below is implementation
> guidance to be tracked as GitHub Issues, not an in-repo checklist.

---

## 10.2.1 Intent

A question is treated as a **research act**, not a help-desk ticket. The interface
makes four things first-class that a single-stream chat box hides:

1. **Plurality** — more than one model can read the same evidence, and the points
   where they diverge are surfaced as signal, not averaged away.
2. **Computation** — the system can write and run code as part of answering, and
   the generated code is visible, editable, and re-runnable.
3. **Provenance** — every claim links to its source passage, including **visual**
   sources (figures, charts, tables), with the source attribution the retrieval
   layer already returns.
4. **Cost truth** — every step is metered to actual dollars and itemised into a
   receipt that can be tagged to a project or grant code.

The unit of work is a **run**: an ordered stream of typed events that the SPA
renders live and can serialise into a reproducible, shareable artifact.

This model reuses the orchestration patterns validated in the reference prototype
(`agent.py`): a cheap router, a parallel multi-model read followed by a third-model
reconciliation, automatic code generation executed in an isolated microVM, and a
running cost meter that closes with an itemised receipt.

---

## 10.2.2 The three modes and the router

The interface exposes three interaction modes. A single cheap **routing call**
(one fast model, `max_tokens≈5`, classified to one word) selects a default mode
for free-form input; the user can always override and force a mode (academics
prefer explicit control). The routing call is metered and appears on the receipt
but is not rendered as an answer step.

| Mode | What the user sees | Execution placement |
|------|--------------------|---------------------|
| **Ask** | A cited synthesis from the project corpus; citations click through to the source passage or figure. | Tier 0 — browser-direct to Bedrock Converse + direct S3 Vectors query (no server hop). |
| **Panel** | One live pane per model (different families and/or weights) reading the **same** evidence in parallel, then a reconciliation pane marking *agreement / disagreement / claims to verify*. | AgentCore Runtime (scales to zero). |
| **Analyze** | The generated Python shown as an editable, re-runnable cell; chart or numeric result rendered inline. | AgentCore Runtime + Code Interpreter microVM. |

Mode keys on the wire mirror the prototype's router vocabulary so existing code
ports directly: `SYNTHESIS` → Ask, `DEBATE` → Panel, `ANALYSIS` → Analyze.

---

## 10.2.3 Mode: Ask

Retrieval-augmented, cited synthesis. Default, lowest-cost path.

- **Placement:** Tier 0. The browser holds short-lived scoped STS credentials
  (vended by the claims→session-tag broker, §3.1) and calls Bedrock Converse and
  S3 Vectors directly. No always-on server component.
- **Provenance:** the system prompt instructs the model to cite each source by its
  stable corpus identifier; the SPA resolves identifiers to in-corpus links
  (`/corpus/<id>`), including deep links to a specific **figure or table** when the
  citation resolves to a visual element (see §10.2.7).
- **Scope:** retrieval is confined to the caller's project corpus by the ABAC
  session tag (`agate:tenant`, `agate:project`); there is no cross-project leakage
  because the vector index itself is selected by tag.

---

## 10.2.4 Mode: Panel (the core differentiator)

Several models read the **same retrieved evidence independently and in parallel**,
then a separate **adjudicator** reconciles them.

Design rules:

- **Heterogeneous roster.** The panel should mix model *families* and *weight
  classes* — e.g. a frontier hosted model alongside an open-weight model served via
  Custom Model Import (§2.5, Rung 1). Independence is the point; two members of the
  same family is a degenerate panel.
- **Same evidence, independent reads.** All members receive the identical evidence
  block and review prompt. They do not see each other's output.
- **Adjudication is structured.** The adjudicator does not write a blended essay;
  it emits a structured reconciliation: where the reviews **agree**, where they
  **disagree**, and which specific claims the reader should **verify**. The
  adjudicator is pinned by a system prompt and a JSON schema (§10.2.5). The SPA
  renders disagreement as a clickable element that shows the conflicting positions
  side by side. Divergence between independent models frequently marks genuinely
  unsettled ground or a weakly-supported claim — that is research-grade signal.
- **Per-pane cost.** Each member streams its own `start`/`done` and per-call cost
  so the UI shows a live, independently-metered pane per model.
- **Placement:** AgentCore Runtime. The orchestration is multi-step, parallel, and
  stateful; it spins up per request and returns to zero afterwards — no idle clock.
  The Strands reference agent (§13.7) wraps the orchestration at the boundary; the
  gateway's identity, cost, Guardrail, and Cedar enforcement sit around it, not
  inside it.

### Reference orchestration (generalised to N members)

This generalises the prototype's dual-review-plus-adjudicator beat to an arbitrary
roster. Python 3.12, threaded fan-out over the same `Backend` and `CostMeter`
interfaces used elsewhere. The adjudication tail parses the structured output
defined in §10.2.5 and emits a `divergence` event.

```python
# panel.py — N-model Panel orchestration.
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from pydantic import ValidationError

from agate.panel.schema import Divergence, strip_fences  # mirrors §10.2.5

Emit = Callable[[dict], None]


def run_panel(
    *,
    backend,            # .converse(tier, system, prompt, max_tokens) -> (text, usage, matches)
    meter,              # CostMeter (thread-safe)
    emit: Emit,
    question: str,
    evidence: str,      # formatted passages: text + multimodal descriptions/refs (§10.2.7)
    roster: list[dict], # [{"tier","label","max_tokens"}, ...] — mixed families/weights
    adjudicator: dict,  # {"tier","label","max_tokens"}
    review_system: str,
    adjudicate_system: str,
) -> dict[str, object]:
    """Run every roster member over the SAME evidence in parallel, then reconcile.

    Each member streams its own start/done + per-call cost so the SPA can render
    one live pane per model (keyed by the `pane` field). Returns {label: review}
    with the structured reconciliation under "__adjudication__".
    """
    lock = threading.Lock()

    def safe_emit(ev: dict) -> None:
        with lock:
            emit(ev)

    prompt = f"Evidence:\n{evidence}\n\nQuestion: {question}"

    def review(member: dict) -> tuple[str, str]:
        tier, label, max_tok = member["tier"], member["label"], member["max_tokens"]
        safe_emit({"type": "model", "tier": tier, "label": label,
                   "state": "start", "pane": label})
        t0 = time.monotonic()
        text, usage, _ = backend.converse(tier, review_system, prompt, max_tok)
        cost = meter.add_llm(f"panel · {label}", tier, label, usage)
        safe_emit({"type": "model", "tier": tier, "label": label, "state": "done",
                   "pane": label, "elapsed_s": round(time.monotonic() - t0, 1),
                   "usage": {"inputTokens": usage.get("inputTokens", 0),
                             "outputTokens": usage.get("outputTokens", 0)},
                   "cost": round(cost, 6)})
        safe_emit({"type": "cost", "total": round(meter.total, 6)})
        return label, text

    reviews: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=len(roster)) as pool:
        for fut in as_completed([pool.submit(review, m) for m in roster]):
            label, text = fut.result()
            reviews[label] = text

    # Reconciliation. The adjudicator returns ONLY JSON conforming to the
    # divergence schema (§10.2.5). Parse and validate defensively; emit a
    # `divergence` event plus a short `answer` carrying the summary.
    transcript = "\n\n".join(
        f"REVIEW — {label}:\n{txt}" for label, txt in reviews.items()
    )
    raw, usage, _ = backend.converse(
        adjudicator["tier"], adjudicate_system, transcript, adjudicator["max_tokens"]
    )
    meter.add_llm("panel · adjudication", adjudicator["tier"], adjudicator["label"], usage)
    safe_emit({"type": "cost", "total": round(meter.total, 6)})

    try:
        payload = json.loads(strip_fences(raw))
        Divergence.model_validate(payload)          # pydantic guard (§10.2.5)
        safe_emit({"type": "divergence", **payload})
        if payload.get("summary"):
            safe_emit({"type": "answer", "title": "Panel — reconciled",
                       "text": payload["summary"]})
    except (json.JSONDecodeError, ValidationError):
        # Adjudicator broke the contract: surface raw text rather than failing.
        safe_emit({"type": "answer", "title": "Panel — reconciled (unstructured)",
                   "text": raw})
        payload = {"summary": raw, "claims": []}

    reviews["__adjudication__"] = payload
    return reviews
```

The roster is configuration, not code: an institution or a user pins which models
sit in the panel (e.g. one frontier model, one open-weight model, one adjudicator)
without changing orchestration.

---

## 10.2.5 Adjudicator contract

The Panel's value depends on the adjudicator producing **structured** output, not
prose, so the SPA can render divergence rather than a wall of text. The adjudicator
is pinned by a system prompt and a JSON schema; the orchestration validates the
output against a Pydantic model before emitting the `divergence` event.

### Adjudicator system prompt (`Q.ADJUDICATE_SYSTEM`)

```text
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
- Set "verify": true for any claim a careful reader should check independently —
  always for "disagreement" and "unsupported", and for "agreement" only when the
  shared support is thin.
- Keep every "note" to at most ~30 words. Where a review cites a source, carry its
  identifier into "evidence_refs".

Output ONLY a single JSON object matching this shape. Emit no prose, no
explanation, and no Markdown code fences — nothing before or after the JSON:

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
```

### `divergence` payload schema (JSON Schema, draft-07)

This is both the adjudicator's required output and the `divergence` event payload
(§10.2.9). Mirror it as the `Divergence` Pydantic model used by `run_panel`.

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Divergence",
  "type": "object",
  "required": ["summary", "claims"],
  "additionalProperties": false,
  "properties": {
    "summary": { "type": "string" },
    "claims": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "text", "kind", "positions", "verify"],
        "additionalProperties": false,
        "properties": {
          "id": { "type": "string" },
          "text": { "type": "string" },
          "kind": { "enum": ["agreement", "disagreement", "unsupported"] },
          "positions": {
            "type": "array",
            "minItems": 1,
            "items": {
              "type": "object",
              "required": ["pane", "stance"],
              "additionalProperties": false,
              "properties": {
                "pane": { "type": "string" },
                "stance": { "enum": ["supports", "disputes", "partial", "silent"] },
                "note": { "type": "string" }
              }
            }
          },
          "verify": { "type": "boolean" },
          "evidence_refs": { "type": "array", "items": { "type": "string" } }
        }
      }
    }
  }
}
```

**Stance semantics:** `supports` — the reviewer asserts or backs the claim;
`disputes` — the reviewer contradicts it; `partial` — the reviewer qualifies or
partially supports it; `silent` — the reviewer did not address it.

**Kind semantics:** `agreement` — every non-silent reviewer supports it;
`disagreement` — reviewers conflict; `unsupported` — asserted by some, corroborated
by none, or weakly grounded. `verify` is always true for `disagreement` and
`unsupported`, and true for `agreement` only when the shared support is thin.

### Worked example

```json
{
  "summary": "The reviews agree PCSK9 inhibition lowers LDL-C, differ on the magnitude of cardiovascular benefit, and one raises a cognitive-safety claim the evidence does not support.",
  "claims": [
    {
      "id": "c1",
      "text": "PCSK9 inhibition lowers circulating LDL-C.",
      "kind": "agreement",
      "positions": [
        { "pane": "Claude Opus", "stance": "supports" },
        { "pane": "Open-weight 70B", "stance": "supports" }
      ],
      "verify": false,
      "evidence_refs": ["PMC4521", "PMC5099"]
    },
    {
      "id": "c2",
      "text": "The magnitude of cardiovascular event reduction is clinically large.",
      "kind": "disagreement",
      "positions": [
        { "pane": "Claude Opus", "stance": "partial", "note": "moderate and trial-dependent" },
        { "pane": "Open-weight 70B", "stance": "supports", "note": "reads the reduction as large" }
      ],
      "verify": true,
      "evidence_refs": ["PMC5099"]
    },
    {
      "id": "c3",
      "text": "PCSK9 inhibitors carry a measurable cognitive-impairment risk.",
      "kind": "unsupported",
      "positions": [
        { "pane": "Claude Opus", "stance": "disputes", "note": "no consistent signal in cited trials" },
        { "pane": "Open-weight 70B", "stance": "silent" }
      ],
      "verify": true,
      "evidence_refs": []
    }
  ]
}
```

### Validation and failure handling

- The orchestration package carries a `Divergence` Pydantic model mirroring the
  schema above, plus a `strip_fences()` helper that removes accidental Markdown
  fences before `json.loads`.
- `run_panel` validates with `Divergence.model_validate` before emitting. On
  `JSONDecodeError` or `ValidationError`, it falls back to an unstructured `answer`
  event carrying the raw text (never a hard failure mid-run).
- Set the adjudicator `max_tokens` generously enough for the full JSON over a large
  panel; the system prompt's ONLY-JSON instruction plus fence-stripping keeps the
  output parseable.
- Tests assert (a) a well-formed adjudicator response yields a `divergence` event
  whose `pane` values are a subset of the roster labels, and (b) a malformed
  response yields the unstructured-answer fallback rather than an exception.

---

## 10.2.6 Mode: Analyze

The model writes Python; an isolated microVM runs it; the code and its result are
both shown.

- **Visible, editable code.** The generated script is emitted as a `code` event and
  rendered as a notebook-style cell. The user can edit it and re-run — researchers
  reason *with* the code, not just consume its output.
- **Execution is isolated.** Code runs in the AgentCore Code Interpreter microVM,
  never on the caller's machine and never in the gateway process. Charts return as
  inline images; numeric/tabular output renders below the cell.
- **Metered as compute.** Execution time is added to the receipt as a compute line
  (`add_compute`), distinct from token costs, so the receipt distinguishes
  inference spend from execution spend.
- **Placement:** AgentCore Runtime + Code Interpreter; scales to zero between runs.

---

## 10.2.7 Multimodal knowledge base

Research corpora are not text. Papers carry figures, plots, micrographs, gels,
schematics, equation images, and tables; datasets arrive as images, audio, or
video. The knowledge base ingests and retrieves these natively.

**Ingestion / processing.** Two supported paths, selected per data source:

- **Native multimodal embeddings** (Amazon Nova Multimodal Embeddings): images,
  audio, and video are embedded directly, preserving visual/temporal context and
  enabling **query-by-image** and visual similarity search. Requires a designated
  multimodal storage S3 bucket for processed artifacts.
- **Parser + text embeddings** (Bedrock Data Automation, or a foundation model as
  parser, paired with a text embedding model): visual and multimedia content is
  converted to text descriptions and embedded as text. Lower fidelity for visual
  similarity, broader regional availability, simpler.

The choice is **orthogonal to the vector store** — both write into the configured
store, which for this system is **S3 Vectors** (§ RAG design). Default to the
native multimodal path where Region and S3 Vectors support permit; fall back to
parser-plus-text-embeddings otherwise.

**Retrieval.** `Retrieve` / `RetrieveAndGenerate` return visual elements alongside
text, with **source attribution for visual elements**. A citation can therefore
resolve to a specific figure or table, and the Ask/Panel panes deep-link to it.
Re-rankers and metadata filters apply across text and visual results.

**Scope.** Each project's knowledge base, its vector index, and its multimodal
storage prefix are selected by the same ABAC session tags (`agate:tenant`,
`agate:project`) that govern model access — one tag scheme, both data scope and model
scope. No new connector surface is introduced.

**Academic payoff.**
- *Figure-aware Ask:* "What does the survival curve in the dose-escalation arm
  show?" retrieves and cites the figure, not just its caption.
- *Query-by-image:* upload a plot or micrograph and retrieve visually similar
  figures across the corpus.
- *Analyze hand-off:* a retrieved table or chart becomes the input to a generated
  script that re-derives or re-plots it, with the original cited as provenance.

**Phase-0 verification (per the design doc's "verify new services" posture):**
confirm, for the target Region, (a) S3 Vectors compatibility with the chosen
multimodal embedding path, (b) the multimodal storage bucket wiring, and (c)
whether L2 CDK constructs exist or an L1 `Cfn*` construct is required for the
multimodal data-source configuration.

---

## 10.2.8 Cross-cutting capabilities

- **Cost receipts as the analytics surface.** The running meter and itemised
  receipt are the primary usage view: actual dollars per run, per project, per
  member, exportable (CSV/JSON) and taggable to a grant or course code.
  Authoritative spend is computed server-side from invocation logs × Price List
  rates (§ spend design); the browser meter is a non-authoritative live estimate.
- **Reproducible run artifact.** Because a run is a typed event stream, "save run"
  serialises the transcript, the panel roster, the generated code, every citation
  (text and visual), the divergence structure, the models used, and the cost into a
  single shareable, citable record. Reproducibility is a research value the artifact
  format should treat as a requirement, not a nicety.
- **Per-project corpus.** Knowledge bases are scoped to a project or course via the
  ABAC tag; a user points a project at its own documents/datasets and the scope
  follows the tag through both retrieval and model access.
- **Model roster.** Frontier hosted models and open-weight models (Custom Model
  Import, §2.5) appear in the same roster and the same Panel. Open weights are
  reproducible and inexpensive — valuable for teaching and for cost-bounded work.
- **Governance at the boundary.** Bedrock Guardrails and Cedar tool policies wrap
  every mode (the prototype demonstrates URL interception and tool-call denial);
  enforcement sits around the agent, not inside it.

---

## 10.2.9 Event protocol additions

Extends the prototype's event protocol (the SPA's streaming contract). New or
extended fields are additive and backward-compatible.

| Event | Change | Purpose |
|-------|--------|---------|
| `model` | add `pane` | Map a model's start/done/cost to a Panel column. |
| `route` | unchanged | Mode selection (`SYNTHESIS`/`ANALYSIS`/`DEBATE`). |
| `divergence` | **new** — payload is the §10.2.5 schema (`summary`, `claims[]` with per-pane `stance` and `verify`) | Structured adjudicator output; drives the side-by-side divergence UI. |
| `citation` | **new** — `{source, modality: text\|image\|table\|audio\|video, ref, thumb?}` | Resolve a claim to a text passage or a specific visual element. |
| `artifact` | **new** — `{run_id, url}` | The serialised reproducible run record. |
| `code`, `chart`, `cost`, `receipt`, `answer`, `guardrail`, `policy_denied` | unchanged | As in the reference prototype. |

The transport stays transport-agnostic (the same emit contract serves WebSocket to
the browser, a CLI runner, and test collectors).

---

## 10.2.10 Architecture placement (NO CLOCKS)

| Surface | Path | Clock? |
|---------|------|--------|
| Ask | Tier 0 — browser-direct Converse + S3 Vectors | None. |
| Panel, Analyze | AgentCore Runtime (per-request microVM, scales to zero) + Code Interpreter for Analyze | None at idle. |
| Adjudication / routing | Bedrock serverless inference | Per-request only. |
| Multimodal KB | S3 Vectors + S3 multimodal storage; serverless ingestion jobs | Per-request / per-ingestion; no idle floor. |

Plain chat never pays for the Runtime. The Panel and Analyze modes spin the Runtime
up per request and it returns to zero. Nothing in this model introduces a
wall-clock-billed idle component; any future component that would must be justified
against the NO CLOCKS principle and flagged by CostMeter as a standing cost.

---

## 10.2.11 Toolchain

Inherits the pinned toolchain (§11): AWS CDK v2 (Python binding) for IaC;
Python 3.12/3.13 for the CDK app and all Lambdas; `uv` for Python; Go for `cli/`.
New Bedrock / AgentCore / S3 Vectors multimodal resources use L1 `Cfn*` constructs
where no L2 exists. The SPA carries swappable transport adapters
(`bedrock.ts` / `agentcore.ts`) so Ask (browser-direct) and Panel/Analyze
(Runtime) share one client surface.

---

## 10.2.12 Implementation phases (track as GitHub Issues)

Open under a milestone (e.g. `academic-interaction-model`). Each issue closes only
when code + tests + a CHANGELOG entry + the issue reference are in place
(Definition of Done, `CLAUDE.md`).

1. **Event protocol + SPA panes.** Implement the additive events (`pane`,
   `divergence`, `citation`, `artifact`); render multi-pane Panel layout and the
   notebook-style Analyze cell. Fakes-only tests for the event stream.
2. **Panel orchestration + adjudicator contract.** `run_panel` (N members +
   adjudicator) on AgentCore Runtime behind the Strands reference agent; per-pane
   cost; the `Divergence` Pydantic model, system prompt, and structured-vs-fallback
   parsing of §10.2.5. Tests with injected fake backend (no AWS), including the
   malformed-adjudicator fallback path.
3. **Analyze cell.** Code generation → Code Interpreter microVM → inline chart +
   editable/re-run. Compute metering line on the receipt.
4. **Multimodal KB.** Multimodal ingestion (native embeddings path with parser
   fallback) into S3 Vectors + multimodal storage; visual source attribution;
   figure/table deep-link resolution; query-by-image. Phase-0 verification gate
   (§10.2.7) first.
5. **Reproducible run artifact.** Serialise/export a run; shareable record;
   grant/course cost tagging on the receipt export.
6. **Router + mode override.** Wire the routing call to default a mode and expose an
   explicit override in the SPA.

Hard stop for review after Phase 2 (the orchestration crux) before proceeding.
