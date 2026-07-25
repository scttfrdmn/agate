# agate Canvas — the integrated reasoning surface

> Status: **design / forward-looking.** This is the *where-it-goes* document for agate's
> interaction surface. Each idea is marked **[built]** (exists today, usually under a different
> framing), **[seam]** (a current primitive that extends naturally), or **[vision]** (proposed,
> not built). The discipline: don't claim more than exists, and show how the vision rests on
> primitives already shipped. Supersedes the "Chat vs Notebook toggle" framing of #185/#200.

## The thesis

Chat and notebook were never two things. They are the same object — a top-to-bottom sequence of
**cells** — seen at two zoom levels. A "chat" is a document whose cells happen to all be prompts;
a "notebook" is one that mixes prompts and code. agate should present **one surface**: a document
of cells that both *reason* (AI, billed, grounded) and *compute* (local Python, free), where the
output of any cell can flow by reference into any later cell.

The current UI conflates two orthogonal axes into one confusing row — a **Chat/Notebook view
toggle** sitting next to a **Mode** selector (Ask/Panel/Analyze) — and stacks a chat composer
below notebook cells that shouldn't be there. Removing the toggle and unifying on cells dissolves
that confusion and, more importantly, unlocks a genuinely differentiated academic tool:
**cost-transparent, reproducible, chained reasoning that interleaves inference and computation.**

Followed to its end (see move #7), this surface is best understood as a **two-layer programming
language wearing a chat**: a deterministic control/data-flow layer (the cell graph — references,
branches, conditionals, merges, loops) over a mixed-determinism instruction layer (each cell body is
a prompt, a code block, or a budget-capped agent). The cost model is its resource type system. The
whole design bet is that this language stays **progressively disclosed** — it *is* a chat until a
user reaches for more. Read move #7 for the full framing; the earlier moves are the buildable steps
that point at it.

## The core model: everything is a cell

**[seam]** agate already has a kind-agnostic cell model (`web/src/chat/notebook.ts`:
`NotebookCell {kind: "prompt" | "code", name, prompt, output/answer, state, stale}`), cross-cell
references (`{{cN}}`, `web/src/chat/dag.ts`), and cost-aware reactivity (code re-runs free;
AI cells stale-mark for explicit billed re-run). The Canvas is the natural completion of that
substrate, not new machinery.

A **Canvas** is an ordered list of cells. Each cell has:

- a stable **id** and a short **name** (`c1`, `c2`, …) — its reference handle;
- a **kind**: `prompt` (AI reasoning, billed), `code` (local Python in the pyodide worker, free),
  or `agent` (a budget/time-capped background agent run — see move #5);
- an **input** (the editable source: a question, or Python);
- an **output** (the answer + citations + receipt for a prompt cell; stdout / last-expr value /
  inline plot for a code cell);
- **dependencies**: the set of `{{cN}}` references its input contains.

There is no "chat mode" and no "notebook mode." **"Chat" is simply the act of appending a prompt
cell at the end of the chain** — the composer at the bottom is the in-progress tail cell. You can
also insert a cell anywhere, make it code or prompt, and reference earlier outputs. One mental
model: *a document of cells that reason and compute, newest at the bottom, composed at the bottom.*

### One model, two renderers (the load-bearing UX rule)

**"Chat is a document of only-prompt cells" is the right *implementation* model and a dangerous
*presentation* model.** It is true for the builder, not the user. Someone asking a quick question
does not want it to become a visible, named `c1` with an editable textarea and a freeze/stale
lifecycle — they want an answer. So:

- **Unify the data model** (everything is a cell underneath), but **ship two renderers over it.**
  A prompt turn renders as a **conversational turn** (read-only answer + citations + one receipt);
  a code cell renders as **cell chrome** (monospace editor, Run, output/plot, `$0.00 (local)`).
- A prompt turn **becomes** a visible, editable, named cell only when the user **edits it or
  references it** — the costume change is a deliberate signal, not a default. Same object, two
  costumes; intent drives which.
- The mental model to sell is **"a chat that can grow a spine,"** not "a notebook that starts
  empty." When a code cell enters (via the composer's Code affordance or "Run this" on an emitted
  block), the document visibly *levels up* — that transition is the teaching moment, and only then
  do prior prompt turns show their `cN` handles.

This is the highest-severity rule for Phase 1 (validated by UX review): if prompt turns ship as
visible editable cells, we rebuild the notebook and *lose* chat — the opposite of the goal.

### Why this is the right UX

- **One mental model**, not two you toggle between. The "which view am I in?" question disappears.
- **Flow state**: you are always at the bottom edge adding the next thought — question or
  computation — with no context switch.
- **Depth is emergent, not imposed**: a plain conversation stays a plain conversation (all prompt
  cells) until computation earns its place. First contact = "just chat"; the computational power
  reveals itself (see "Run this" below).
- **Chat-familiar scrolling**: newest at the bottom, compose at the bottom — but every entry is an
  addressable, re-runnable, referenceable cell.

## The integration moves

Ordered roughly by leverage and by how far they reach — moves 1–4 are near-term and rest directly
on shipped primitives; 5–7 are the longer arc toward the two-layer language. Each cites the
primitive it extends.

### 1. "Run this" — the AI's code becomes a live cell **[seam]**

Prompt cells already return Markdown containing ` ```python ` fences (today those get a Copy
button, `render/markdown.ts addCopyButtons`). The move: give every code block in an AI answer a
**Run** action that spawns a **code cell seeded with that code**, right below. "LLM writes code"
isn't a new mode — it *emerges* from what the model already emits. Ask *"plot Gibbs free energy vs
temperature"*, the AI writes the code, you click Run, pyodide renders the plot inline as a new
cell. Minimal mechanism, maximal integration; human-in-the-loop (never auto-run emitted code).

### 2. Result → prompt — the reasoning loop **[seam]**

A code cell's output — a number, a table, a **plot image** — is referenceable by a later prompt
via `{{cN}}` (built for text; images lean on the **Nova-2 multimodal embedding** already pinned in
`agate-data`, `#238`). So: `c1` AI writes code → `c2` runs it → plot → `c3` *"at what temperature
does ΔG cross zero, given {{c2}}?"* and the AI reasons about the **result**. The closed loop —
**AI → code → result → AI**, each feeding the next — is the differentiated academic instrument:
reasoning interleaved with real computation.

### 3. Cost-aware chained reasoning, made visible **[built]**

Because cells reference each other and reactivity propagates (`dependentsOf`): editing an upstream
cell re-runs downstream **code** cells silently (free, local WASM) and **stale-marks** downstream
**AI** cells for an explicit, billed re-run — reactivity never spends tokens by accident. A Canvas
*is* an editable reasoning pipeline, and the cost meter shows exactly where money goes:
**writing code = billed, running it = free, interpreting a result = billed.** Cost-transparent
reasoning pipelines are novel; NO CLOCKS makes agate their natural home.

### 3a. Per-cell receipts, always visible **[built → refine]**

Every prompt cell already carries a receipt (`renderReceipt`: `inputTokens in / outputTokens out`
and a per-question cost). The Canvas makes this a **standing, quiet affordance on each cell** — small
muted text under each prompt cell showing *its own* input/output token counts and cost, not just a
running session total. The taxi-meter (running session spend + budget bar) stays; the per-cell line
answers "what did *this* thought cost?" at a glance. Code cells show **$0.00 (local)** so the
free-vs-billed distinction is visible inline. This is the granular complement to the aggregate meter
and the cost trail of move #3.

### 4. The composer is just the next cell **[vision]**

The fix for "why is stuff above the prompt?": in a document, newest work is at the bottom and you
author at the bottom edge — that's the intuitive part. The pinned bottom input is the in-progress
tail cell. A single inline switch on it — **Ask · Code** (default Ask) — picks the kind. That is
the only view-level control needed. Mode (Panel/Analyze/patterns) and Model remain, but stop
competing with a redundant view toggle, which is **removed**.

### 5. Budget/time-capped agent cells — "spend it, don't just meter it" **[seam → vision]**

The taxi-meter measures spend *after the fact*. The next step turns the budget into an **input to
the work**: a third cell kind — an **agent cell** — that launches a background agent to research or
compute an answer, **bounded by a cost cap and/or a time cap the user sets on the cell.** The agent
is told its ceiling and must **budget itself** — decide how many searches / model calls / tool
invocations it can afford — to deliver the best answer within the envelope. "Do the literature scan
for $2 and 5 minutes, then report what you have" is a first-class, governed request.

Why agate is the right place for this (not just another agent runner):

- **The budget cascade already exists and fails closed** (`cost/precall.py evaluate_priced_cascade`,
  #81): a pre-call gate that declines the *next* step once accumulated spend would cross a budget —
  "what a gate allows cannot, by itself, exceed budget." An agent cell's cap is **another node in
  that same cascade**, scoped to the cell. So the cap isn't advisory text in a prompt (which an LLM
  can ignore) — it is **enforced by the same server-side pre-call check** that already gates every
  Tier-1 call. The agent *also* sees its remaining budget/time as context so it can plan, but
  enforcement never depends on the agent's good behaviour.
- **Agents run on AgentCore Runtime** (`agent/`), serverless, scale-to-zero — a background agent
  cell holds no standing cost while idle (NO CLOCKS), and its bound is real money/time, not a
  fixed reservation.
- **Attribution + audit** are already server-side (spend computed from Bedrock invocation logs);
  an agent cell's sub-spend rolls up under the same tenant/scope ABAC tags.

The cell's lifecycle, in the Canvas:

```
  agent cell:  question + cap (e.g. $2.00 and/or 5 min)
     │  launch (background; the cell shows "running", live spend/time against the cap)
     ▼
  AgentCore run — plans within the envelope, self-budgets its searches/calls/tools
     │  each step passes the SAME pre-call budget cascade (cell cap = a cascade node)
     │  when the cap is hit: stop, return best-effort answer + a note that it was cap-bounded
     ▼
  output = the answer + a receipt: actual $ spent / time used / steps taken, vs the cap
```

The output is a **frozen cell result** like any AI cell (see reproducibility below) — referenceable
by later cells via `{{cN}}`, so an agent cell's finding feeds the next prompt/code cell. Editing the
question or cap **stale-marks** it for an explicit, billed re-launch (never auto-run — a background
agent can spend real money).

**What's built vs. new:** the budget cascade, the receipt, AgentCore-scoped credentials, and
scale-to-zero are **[built]**; the pieces to add are **(a)** a per-invocation cost/time cap threaded
into the agent runtime as both a *planning input* and a *cascade node*, **(b)** the agent's
self-budgeting behaviour (plan → check remaining → decide next step), and **(c)** the agent-cell UI
(cap inputs, live spend/time-against-cap, cap-bounded result). Background/async execution + progress
streaming is the main new runtime surface.

**Governed external reach (the escape hatch).** A research agent needs to reach outside the tenant,
and agate already has the governed door: **`web-fetch`** is a declared, clampable agentspec
capability (`agate/agentspec.py`) — "fetch one allowlisted HTTPS URL, read-only, **off unless
explicitly granted**." It flows through the **Cedar `call_tool` policy** (`policy/cedar.py` —
`resource.tool in principal.allowed_tools`), so Cedar decides *whether this principal may use the
tool at all*, and its **effect is bounded server-side** by four independent layers: (1) an
institution **host allowlist**, default-deny; (2) the **SSRF guard** (https-only, no
private/IMDS/CGNAT hosts, socket pinned to the validated IP, no private redirects); (3) the
**budget cascade** (a fetch is a priced action); (4) tenant/scope ABAC on the acting credential.
This is the same claims→tags→Cedar pipeline that fences *data*, now fencing *web reach* — exactly
the governed escape hatch an agent cell should use.

**Gap this surfaces: web *search*, not just fetch.** Today only `web-fetch` (one allowlisted URL)
exists — there is **no `web-search` capability**. A capped research agent ("scan the literature for
$2") needs search, not just fetch-a-known-URL. Adding a `web-search` capability (same Cedar-gated,
allowlist/budget-bounded pattern, pointed at an institution-approved search endpoint) is a
prerequisite for agent cells to do genuine research, and should be designed with the same
default-deny discipline.

**Hard parts specific to this:**
- **Enforcement vs. self-governance are different guarantees.** The agent is *told* its budget so it
  can plan well, but the cap is *enforced* by the pre-call cascade regardless — a confused or
  adversarial agent still cannot overspend. Keep these two clearly separate in the design.
- **Time caps are softer than cost caps.** Cost is enforced pre-call exactly; a wall-clock cap can
  only stop *starting* new steps (bounded overrun on an in-flight step), mirroring the soft-cap
  "decline to start the next call" rule — not an in-flight kill.
- **Partial results are the norm, not the error.** A cap-bounded agent returning "here's what I
  found within $2 / 5 min, and what I'd do with more" is success, and the receipt must make the
  boundary honest.

### 6. Branch & merge — the Canvas is a DAG, not a line **[seam → vision]**

The linear "newest at the bottom" column is the *default read*, but the moment cells reference each
other (`{{cN}}`) the Canvas is **already a dependency DAG** (`dag.ts buildDeps`/`dependentsOf`). The
next conceptual leap is to make branching and merging first-class: a cell can **fan out** into
several parallel continuations, and a later cell can **merge** them.

This is not speculative — **Panel is already a branch+merge**: fan out the same question to N models,
merge with an adjudicator (`agent/server.py` DEBATE roster + adjudicator). Branch/merge simply
generalizes that pattern from a hidden agent-side behaviour to a Canvas-level structure the user can
compose:

- **Branch (fan-out).** From one cell, spawn parallel branches: the same prompt to different models
  (a Panel), or genuinely different follow-ups explored side by side ("try the thermodynamic
  argument in branch A, the kinetic one in branch B"), or N budget-capped agent cells racing the
  same question under different caps. Branches run independently (and, for code/agent branches,
  concurrently).
- **Merge (fan-in).** A merge cell takes several branch outputs as inputs (`{{a3}}`, `{{b3}}`, …)
  and combines them — an adjudicator prompt that synthesizes, a code cell that diffs/aggregates
  results, or a predicate that selects a winner.
- **Predicate / conditional cells.** A cell whose output is a boolean/selection gates which branch
  continues — "if {{c4}} shows convergence, proceed; else branch to refine." This turns the Canvas
  into a genuine reasoning *program*, not just a transcript.

Why this composes with everything above:
- **Reactivity already handles it.** `dependentsOf` is a topological walk over the DAG; branch/merge
  is just a DAG with width. Editing a pre-branch cell stale-marks *all* downstream branches (billed
  AI cells wait for explicit re-run; code branches recompute free) — the cost-aware rule scales to
  the graph unchanged.
- **Panel becomes composable.** Instead of Panel being an opaque mode, a user can *build* one:
  branch a prompt across chosen models (ties to #237's per-cell model choice), merge with an
  adjudicator cell they can read and edit. The governance (each branch is a real, budgeted,
  ABAC-scoped call) is unchanged.
- **Cost is per-branch and visible.** Each branch carries its own receipt (#3a); the merge shows the
  total. "This three-model panel cost $0.04; the kinetic branch was the expensive one."

**Hard parts:**
- **Presentation of a non-linear graph in a linear-first UI.** The default column read must survive
  — branches likely render as indented/side-by-side sub-columns under their fork point, collapsible,
  with the merge pulling them back to the main spine. Getting this legible without becoming a
  node-graph editor (the "just chat" first contact must hold) is the core UX risk.
- **Combinatorial cost.** Fan-out multiplies spend; branches of AI/agent cells need the same
  never-auto-run discipline, and a branch-level cap (a natural fit with move #5's caps — cap the
  *whole branch set*, not just one cell).
- **This is the boundary where "linear chat-anchored" may no longer be enough** — see the
  spatial-vs-linear open question, which branch/merge sharpens rather than settles.

### 7. Loops, and the two-layer language **[vision]**

Follow the thread: references give **data flow**, branches give **fan-out**, predicates give
**conditionals**, merges give **fan-in**. The one remaining control-flow primitive is the **loop** —
"keep refining until {{c7}} says the argument converges," "for each document in {{c3}}, summarize
it," "retry the agent with a bigger cap until it finds an answer or hits $10." Add loops and the
Canvas stops being a document and becomes a **program**.

The honest framing of the whole surface, then, is a **two-layer programming language**:

- **Layer 1 — control/data flow (deterministic):** the cell graph itself — references, branches,
  predicates, merges, loops. This is ordinary, inspectable, reproducible program structure. It runs
  the same way every time; a `git diff` of it is a diff of the program.
- **Layer 2 — the cell bodies (mixed determinism):** what each cell *does* — a **prompt** (a
  non-deterministic inference, frozen once run), a **code** cell (deterministic local compute), or an
  **agent** cell (a budget-capped non-deterministic sub-process). The values flowing between cells are
  the interface between the two layers.

This is the real thesis the rest of this doc circles: **agate Canvas is a small, cost-governed,
partly-non-deterministic dataflow language whose "instructions" are prompts, code, and agents, and
whose surface is a chat.** That framing explains why the earlier pieces cohere:

- **Reproducibility (freeze-on-run)** is exactly what makes a non-deterministic *instruction* behave
  in a deterministic *program*: the frozen output is the instruction's committed result; re-running is
  re-evaluating that instruction, explicitly.
- **The cost cascade** is the language's **resource type system** — every instruction has a price,
  budgets are enforced pre-call, and agent cells are budget-scoped sub-programs. NO CLOCKS means the
  runtime itself has no idle cost, so a program is only as expensive as the instructions it actually
  runs.
- **Branch/merge/loops** are the language's control flow; **Panel** is a library function
  (fan-out-to-models + adjudicate) expressible in it.

**The discipline this demands (and the trap to avoid):** a full visual dataflow language is a
notorious UX tar pit — powerful, and almost always unusable for newcomers. The entire bet of this
document is that the language stays **progressively disclosed**: it reads and writes as a chat by
default; a reference, a branch, a loop each appear only when a user reaches for that power, and the
program structure is always *legible as a document* rather than presented as a node-graph IDE. If we
ever have to choose between "more expressive" and "still feels like chat," **chat wins** — the
language is the ceiling, not the entry point. Loops in particular must be bounded (an iteration cap
and/or a budget cap, reusing move #5) so a runaway loop can't silently spend.

**[vision]** — none of the control flow beyond simple references is built; this section names the
destination so the earlier, buildable moves (1–6) are chosen to point at it rather than away from it.

## The code-cell environment is a curated internal repository **[built]**

A consequence of the security frame worth stating plainly: code cells have **no public
`pip install` / PyPI / CDN reach at runtime.** The pyodide runtime and its package wheels
(`ROOT_PACKAGES` in `web/scripts/copy-pyodide.mjs` — today numpy/pandas/matplotlib + their closure)
are **vendored at build time from a pinned pyodide release and served from agate's own origin**
(`packageBaseUrl` pinned to `/pyodide/`, no CDN fallback). So agate *is* its own **curated,
self-hosted package repository** for the code environment — an institution-reviewed allowlist of
libraries, not a proxy to the open index. Adding scipy/astropy/etc. is a config change to
`ROOT_PACKAGES` (the institution curates its set), expressed as build-time vendoring rather than a
running package-server (keeps NO CLOCKS — no always-on Artifactory/proxy). This mirrors, on the
code side, the same default-deny egress posture that `web-fetch` applies on the network side.

## Reproducibility & git (marimo-style) **[vision]**

A Canvas should be a **plain, diff-able, git-committable artifact** — like marimo notebooks are
`.py` files. Two requirements, one of them subtle and important:

1. **Serializable to a readable file.** A Canvas serializes to a single document (JSON today via
   `notebook-store.ts`; a friendlier text/`.py`-like format is a candidate) capturing each cell's
   kind, name, input, and last output. This already works for save/open to the corpus `_notebooks/`
   prefix; git is the same content in the working tree.

2. **Non-deterministic outputs freeze once run.** This is the crux the user named. Code cells are
   deterministic-ish and can re-run freely. **Prompt cells are NOT** — the same prompt yields
   different text each inference. So a committed Canvas must treat a prompt cell's output as a
   **frozen, static value** — the exact answer produced when it last ran — and *never silently
   re-infer*. Re-inference happens **only on an explicit user action** (a "re-run" click) or when a
   dependency the user chose to propagate changes and they confirm the billed re-run. In git terms:
   the frozen answer is part of the committed artifact and changes only when a human causes a new
   inference — so a `git diff` reflects a real, intended re-run, not nondeterministic churn. This is
   the same discipline as the existing **stale-marking** rule (AI cells never auto-fire), extended
   to persistence: **stale ≠ automatically re-run; frozen output stays until a human re-runs.**

### Freeze/stale state machine (per prompt cell)

```
  fresh  ──(edit this cell OR an upstream dep changes)──▶  stale
    ▲                                                        │
    └──────────────(explicit, billed re-run)────────────────┘
  committed to git: the FROZEN output travels with the cell; `stale` is a flag, not a
  trigger. Opening a committed Canvas shows the frozen answers verbatim — no inference,
  no cost — with stale cells badged so the user can choose to refresh.
```

This gives reproducible, shareable reasoning documents whose AI outputs are stable under version
control, while preserving the cost-aware "never spend tokens silently" guarantee.

## The hard parts (not hand-waved)

- **Scroll model is make-or-break.** Chat (bottom-anchored, autoscroll to newest) and notebook
  (free, top-down) genuinely fight. Decision: **chat-style anchoring** — compose at the bottom,
  newest at the bottom, autoscroll on append — with cells addressable within that column. Get this
  wrong and the whole surface feels wrong.
- **When does an answer become a cell vs stay inline?** A one-off Q&A must not fragment. Keep
  prompt+answer as **one** cell (the answer is the cell's output, as today); only **spawn a new
  code cell** when the user Runs an emitted block. Structure appears only when it earns its place.
- **Never auto-run AI-written code.** Explicit Run only, in the sandboxed pyodide worker (no
  network, no server) — safe *and* free. Aligns with the cost-aware rule.
- **Discoverability.** Must be usable as "just chat" on first contact; the "Run this" affordance on
  an emitted code block is the teaching moment that reveals the computational depth.
- **Security posture unchanged.** Prompt cells route through the same gated/metered chokepoint +
  ABAC boundary; code runs client-side WASM with no egress; the only untrusted-HTML sink remains
  the reviewed `renderInto`. The Canvas is a reorganization of the surface, not a new trust
  boundary.

## Relationship to what exists

- **Supersedes** the Chat/Notebook *toggle* (#185, #200) — those shipped the cell model, code
  execution, references, reactivity, and save/open, which are exactly the Canvas substrate. The
  Canvas removes the toggle and unifies the surface on top of them.
- **Folds in** #236 (the UX confusion this doc's model resolves), #237 (Panel model choice becomes
  a per-prompt-cell control), and #238 (result→prompt, incl. images via multimodal embed).
- **Not** an agent-platform change — this is the human-facing reasoning surface (design §10.2),
  distinct from the AgentCore agent path.

## Proposed phasing

1. **Unify the surface** — remove the Chat/Notebook toggle; one document of cells; the composer is
   the next cell with an **Ask · Code** switch; fix the composer-hide bug. Mostly deletion + reflow;
   directly resolves the reported confusion.
2. **"Run this"** — a Run action on code blocks in AI answers spawns a seeded code cell. Small,
   high-wow, makes "LLM writes code" real.
3. **Result → prompt loop** — code output (incl. plot images) referenceable by later prompt cells
   via `{{cN}}` + the multimodal embed (#238).
4. **Per-cell receipts** — standing per-cell token/cost line (billed) or `$0.00 (local)` (code),
   complementing the running meter (move #3a). Small, mostly built.
5. **Reproducibility / git** — a readable serialized format + the freeze-on-run discipline for
   non-deterministic outputs; commit/round-trip fidelity.
6. **Chained-reasoning polish** — stale/re-run affordances, the cost trail, Panel-per-cell (#237).
7. **Budget/time-capped agent cells** — the new agent cell kind (move #5): per-invocation cap
   threaded as both a planning input and a cascade-enforced node, self-budgeting agent behaviour,
   background execution + progress streaming, cap-bounded result. Largest new runtime surface;
   builds on moves 1–3.
8. **Branch & merge** — promote the reference-DAG to a first-class branch/fan-out + merge/fan-in
   structure (move #6), with predicate/conditional cells; generalize Panel into a composable
   branch+merge. Most advanced; the point at which the linear-vs-spatial UI question must be
   settled.
9. **Loops + the language** — the final control-flow primitive (move #7), always bounded by an
   iteration and/or budget cap. Reaching here means the Canvas is a two-layer language; only pursue
   it if moves 1–8 have kept the "still feels like chat" bet intact. The destination, not a
   near-term deliverable.

## Phase 1 UX spec (from UX review)

Concrete decisions for the unification, validated by a UX-designer review. Build to these:

- **Two renderers, one model** (see "One model, two renderers" above) — the load-bearing rule.
- **Empty state = a chat app.** Composer only. No `+Prompt`/`+Code`/Save/Open chrome in an empty
  doc (that screams "notebook" and kills first-contact simplicity). Gate those on content existing.
- **Composer `Ask · Code` is Ask-weighted, not a 50/50 toggle.** Ask is the default state; Code is a
  quiet mode-in (a `</>`/`{}` affordance or `/code`), not an equal peer tab — an equal toggle tells
  users "this is a code tool" and undermines "just chat."
- **Sticky-mode reset (day-one bug if missed):** after submitting a code cell, reset the composer to
  Ask, so the next typed question isn't sent as Python. Mode must be visually unambiguous per
  submission.
- **Autoscroll discipline:** newest-at-bottom + autoscroll on stream, BUT if the user has scrolled
  up, do not yank them down — show a "↓ new" pill (standard chat pattern). Notebook users scroll up
  to read; don't fight them. Build this in Phase 1, not later.
- **Progressive disclosure — visible to a newcomer:** composer + Ask/Code (Ask-weighted) + Model
  (quiet) + streaming indicator + answer/citations/receipt. **Hidden until reached for:** `+Code`
  (reach via the switch or "Run this"), `{{cN}}` refs and cell **names** (auto-assigned but not
  shown until ≥2 cells AND a referential action), per-cell **re-run** (hover/overflow on prompt
  turns; always shown on free code cells), Save/Open (appear once ≥1 turn), the stale/frozen badge
  (only after an edit creates staleness).
- **One receipt per turn.** A prompt turn's answer already has a receipt; do NOT add a second
  standing per-cell cost line on top of it (reconciles move #3a — the standing `$0.00 (local)` line
  is for *code* cells, which otherwise have no receipt).
- **Mode (Ask/Panel/Analyze) demoted, or at minimum labeled + visually separated from Model.** An
  unlabeled dropdown next to Model is the actual reported bug — don't carry it forward. Model is a
  legitimate per-turn control near the composer; Mode is better as an action invoked on a prompt
  (a "get a panel" choice / `/panel`) than a global mode you're persistently *in* (Panel is a
  library function, move #6). Full Mode refactor can wait, but the label/separation cannot.

## Open questions

- **File format for git.** JSON (works today) vs a marimo-like `.py`/text format that diffs
  cleanly and is human-editable outside agate. The freeze-frozen-output requirement complicates a
  pure-`.py` format (where does the frozen answer live? a sibling output file? an embedded block?).
- **Spatial vs linear — sharpened by branch/merge (move #6).** This doc assumes a linear,
  chat-anchored column as the default read. Branch/merge is the strong reason to revisit: parallel
  branches want *some* 2D expression (indented/side-by-side sub-columns under a fork, collapsing
  back at the merge). The bet to test: can branches be a **collapsible, mostly-linear** embellishment
  (default view stays a single spine; expand a fork to see its branches) rather than a full 2D
  node-graph editor — preserving "just chat" first contact while supporting real DAGs? If that bet
  fails, branch/merge may force a genuinely spatial canvas, which is a larger product decision.
- **How much of the DAG to surface.** Today references are textual (`{{cN}}`); a visible dependency
  graph could help for large Canvases but risks over-complicating the "just chat" first contact.
- **Agent-cell defaults & discovery.** What's a sensible default cap ($ / time)? How does a user
  escalate a cap-bounded result ("that wasn't enough — here's $5 more")? Does re-launching with a
  bigger cap resume or restart? Where do intermediate agent findings surface while it runs (a live
  sub-stream inside the cell)?
- **Time-cap semantics.** Wall-clock is a soft bound (stop starting new steps); is that enough, or
  do some workflows need a hard deadline that abandons an in-flight step? Start soft.
- **`web-search` capability.** Agent cells need governed *search*, not just `web-fetch` (one URL).
  Design a `web-search` capability on the same Cedar-gated, allowlist/budget-bounded, default-deny
  pattern — pointed at an institution-approved search endpoint. Prerequisite for real research
  agents; not built.
- **Curated package set per institution.** `ROOT_PACKAGES` is a global default today. Should the
  vendored library set be per-tenant/per-scope (a chemistry course gets rdkit; a stats course gets
  statsmodels)? That's an institutional-curation UX + a per-tenant build/serve question.
