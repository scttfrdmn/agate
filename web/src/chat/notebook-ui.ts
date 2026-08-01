// The Canvas cell renderer (#242, formerly the notebook view #185) — ONE data model, TWO
// renderers. A `prompt` cell is a chat turn: by default it renders as a read-only conversational
// answer (question + rendered Markdown + citations + one receipt), exactly like the chat
// transcript. It grows its editable *cell* chrome (a textarea + Run + the {{cN}} handle) only when
// the user reaches for it — editing/expanding it, or another cell referencing it. A `code` cell
// always renders as a cell (monospace source + local-run output + a $0.00 (local) line). This is
// the load-bearing "chat that can grow a spine, not a notebook that starts empty" rule.
//
// The markdown/math/citation rendering is delegated to render/markdown.ts (the XSS boundary);
// the Sources/receipt/copy markup is the exact chat/ui.ts helpers, reused verbatim.

import type { AgentCap, CellKind, Notebook, NotebookCell } from "./notebook";
import { referencedNames } from "./dag";
import { copyAnswerBtn, renderReceipt, renderSources } from "./ui";
import { renderInto } from "../render/markdown";
import { modelLabel } from "../router";

export interface NotebookCallbacks {
  onRun?: (cellId: string, prompt: string) => void;
  onRunCode?: (cellId: string, code: string) => void;
  // Launch an agent cell with its authored cap (#248). Billed — never auto-run.
  onRunAgent?: (cellId: string, prompt: string, cap: AgentCap) => void;
  // Cancel an in-flight agent run (aborts the client wait; the server self-bounds by its cap).
  onCancelAgent?: (cellId: string) => void;
  // The agent cell's cap changed in the editor (cost/time/steps) — stales it like a model change.
  onSetCap?: (cellId: string, cap: AgentCap) => void;
  onAddCell?: (kind: CellKind) => void;
  // The cell's source changed in the editor — used to stale-mark dependents (#200 slice 3).
  onEdit?: (cellId: string, source: string) => void;
  // Reveal ("Edit") or collapse a prompt turn's editable cell chrome — the two-renderer costume
  // change (#242). A no-op for code cells (always shown as cells).
  onToggleExpand?: (cellId: string, expanded: boolean) => void;
  // "Run this" (#243): a Run button on a python block in an answer spawns a code cell seeded with
  // that code, inserted directly below the answering cell (`afterCellId`), then runs it.
  onRunFromAnswer?: (afterCellId: string, code: string) => void;
  // Per-cell model pin (#247/#237): the cell chose model `modelId` (""/undefined = follow the
  // composer's Model). The entitled options for the picker are supplied via `modelOptions`.
  onSetModel?: (cellId: string, modelId: string | undefined) => void;
  modelOptions?: ReadonlyArray<{ value: string; label: string }>;
  // Save / open the whole Canvas to the corpus store (#200 slice 4). Omitted when the corpus
  // endpoint isn't configured; only shown once the document has content (progressive disclosure).
  onSave?: () => void;
  onOpen?: () => void;
}

function el(tag: string, cls: string): HTMLElement {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
}

// The "Thinking …" indicator — identical markup to the chat transcript's (chat/ui.ts), so a
// running turn looks the same in both renderers (Phase-1 unity; reported inconsistency).
function thinkingIndicator(): HTMLElement {
  const thinking = el("div", "thinking");
  const label = el("span", "thinking-label");
  label.textContent = "Thinking";
  const dots = el("span", "thinking-dot");
  dots.innerHTML = "<span></span><span></span><span></span>";
  thinking.append(label, dots);
  return thinking;
}

// Whether a prompt cell should render in its editable *cell* costume rather than the read-only
// chat costume. It earns cell chrome when: it has no answer yet (it IS the working cell), the user
// expanded it, it's marked stale (needs an explicit re-run), or another cell references it (its
// {{cN}} output is load-bearing, so its handle + source must be visible).
function showsAsCell(cell: NotebookCell, referenced: Set<string>): boolean {
  // Code + agent cells always wear cell chrome: their controls (code source / cap inputs + a live
  // spend readout) must stay visible, never collapse into a read-only chat bubble.
  if (cell.kind === "code" || cell.kind === "agent") return true;
  if (!cell.answer) return true;
  // While a turn is re-running or has errored, show cell chrome so the thinking indicator / error
  // is visible (the chat costume only renders a settled answer).
  if (cell.state !== "idle") return true;
  if (cell.expanded || cell.stale) return true;
  return cell.name ? referenced.has(cell.name) : false;
}

/** Render the whole Canvas into `target` (replacing its content). */
export function renderNotebook(
  nb: Notebook,
  target: HTMLElement,
  cb: NotebookCallbacks = {},
): void {
  target.replaceChildren();
  const referenced = referencedNames(nb.cells);
  // Cell names/handles are disclosed only once the graph is real — i.e. at least two cells AND a
  // referential action ({{cN}}) somewhere. Until then a lone answer is "just chat," not "c1".
  const showNames = nb.cells.length > 1 && referenced.size > 0;

  // Save / Open toolbar (only when persistence is wired AND the doc has content). Progressive
  // disclosure: an empty/one-turn scratch surface shouldn't sprout notebook chrome.
  if ((cb.onSave || cb.onOpen) && nb.cells.length > 0) {
    const bar = el("div", "notebook-toolbar");
    if (cb.onSave) {
      const save = el("button", "btn ghost btn-sm notebook-save") as HTMLButtonElement;
      save.type = "button";
      save.textContent = "Save";
      save.addEventListener("click", () => cb.onSave?.());
      bar.appendChild(save);
    }
    if (cb.onOpen) {
      const open = el("button", "btn ghost btn-sm notebook-open") as HTMLButtonElement;
      open.type = "button";
      open.textContent = "Open";
      open.addEventListener("click", () => cb.onOpen?.());
      bar.appendChild(open);
    }
    target.appendChild(bar);
  }
  // Cost trail (#247, move #3): the Canvas's own running total across the chain — the sum of each
  // answered cell's receipt — distinct from the session meter. Shows where money went in THIS
  // document. Prompt cells bill via `meta.cost`; agent cells (#248) spend real money via
  // `agentReceipt.spentUsd` — count both, or the total undercounts. Code cells are free.
  const cellCost = (c: NotebookCell): number | undefined => {
    if (c.kind === "prompt" && typeof c.meta?.cost === "number") return c.meta.cost;
    if (c.kind === "agent" && c.agentReceipt) return c.agentReceipt.spentUsd;
    return undefined;
  };
  const billed = nb.cells.filter((c) => typeof cellCost(c) === "number");
  if (billed.length) {
    const total = billed.reduce((sum, c) => sum + (cellCost(c) ?? 0), 0);
    const staleCount = nb.cells.filter((c) => c.stale).length;
    const trail = el("div", "notebook-cost-trail");
    const billedLabel = `${billed.length} billed cell${billed.length === 1 ? "" : "s"}`;
    trail.textContent = `Canvas cost: $${total.toFixed(6)} · ${billedLabel}`;
    if (staleCount) trail.textContent += ` · ${staleCount} stale (re-run to refresh)`;
    trail.title =
      "Total cost of the AI cells in this Canvas (code cells are free), reflecting each cell's most " +
      "recent run. Distinct from the session meter, which is cumulative spend.";
    target.appendChild(trail);
  }
  // The reference hint only makes sense once names are shown.
  if (showNames) {
    const hint = el("div", "notebook-hint");
    hint.textContent =
      "Tip: reference another cell's output with {{c1}}, {{c2}}, … Editing a cell marks dependents stale; code cells re-run automatically.";
    target.appendChild(hint);
  }
  const list = el("div", "notebook");
  for (const cell of nb.cells) list.appendChild(renderCell(cell, cb, referenced, showNames));
  target.appendChild(list);
  // No persistent "+ Prompt / + Code" add-bar: the composer (Ask · Code) is the append path
  // (#242). Structure appears when it earns its place, not as standing chrome.
}

function renderCell(
  cell: NotebookCell,
  cb: NotebookCallbacks,
  referenced: Set<string>,
  showNames: boolean,
): HTMLElement {
  if (cell.kind === "code") return renderCodeCell(cell, cb, showNames);
  if (cell.kind === "agent") return renderAgentCell(cell, cb, showNames);
  return showsAsCell(cell, referenced)
    ? renderPromptCell(cell, cb, showNames)
    : renderChatTurn(cell, cb);
}

// A cell header: its {{cN}} reference name (only when names are disclosed) + a "stale" badge when
// an upstream input changed since this cell last ran (prompt cells only — code dependents
// auto-re-run, so they clear it).
function renderCellHeader(cell: NotebookCell, showNames: boolean): HTMLElement {
  const head = el("div", "notebook-cell-head");
  if (cell.name && showNames) {
    const name = el("span", "notebook-cell-name");
    name.textContent = cell.name;
    name.title = `Reference this cell's output as {{${cell.name}}} in another cell`;
    head.appendChild(name);
  }
  if (cell.stale) {
    const stale = el("span", "notebook-cell-stale");
    stale.textContent = "stale — re-run";
    stale.title = "An input changed since this cell last ran";
    head.appendChild(stale);
  }
  return head;
}

// The CHAT costume: a read-only conversational turn (question bubble + rendered answer + citations
// + one receipt), matching chat/ui.ts. An "Edit" affordance (hover/focus-revealed) flips the cell
// into its editable costume without a re-run; "Re-run" re-asks it. This is the default look for an
// answered prompt cell — the surface reads as a chat until the user reaches for the cell.
function renderChatTurn(cell: NotebookCell, cb: NotebookCallbacks): HTMLElement {
  const wrap = el("div", "notebook-cell notebook-cell-chat");
  wrap.dataset.cellId = cell.id;
  wrap.dataset.kind = "prompt";

  const pair = el("div", "msg-pair");
  if (cell.prompt.trim()) {
    const userBubble = el("div", "user-bubble");
    const qBadge = el("div", "bubble-badge");
    qBadge.textContent = "You asked";
    const qText = el("div", "q-text");
    qText.textContent = cell.prompt; // verbatim, never markdown
    userBubble.append(qBadge, qText);
    pair.appendChild(userBubble);
  }

  const asst = el("div", "assistant-bubble");
  const head = el("div", "bubble-head");
  const aBadge = el("div", "bubble-badge");
  aBadge.textContent = "Answer";
  head.appendChild(aBadge);
  // Show which model produced this answer (parity with the chat transcript) — matters under Auto,
  // where the routed model isn't otherwise visible. `routed` styling + rationale tooltip when known.
  if (cell.meta?.modelId) {
    const modelTag = el("div", "model-tag");
    modelTag.textContent = modelLabel(cell.meta.modelId);
    if (cell.meta.modelReason) {
      modelTag.title = `Auto-routed: ${cell.meta.modelReason}`;
      modelTag.classList.add("routed");
    }
    head.appendChild(modelTag);
  }
  asst.appendChild(head);
  const body = el("div", "answer-body");
  renderInto(body, cell.answer ?? "", `${cell.id}-`, {
    onRunCode: cb.onRunFromAnswer ? (code) => cb.onRunFromAnswer?.(cell.id, code) : undefined,
  });
  body.classList.add("rendered");
  asst.appendChild(body);
  if (cell.sources && cell.sources.length) asst.appendChild(renderSources(cell.sources, `${cell.id}-`));
  // One receipt per turn (the Phase-1 rule) — the answer's own receipt, no extra per-cell line.
  if (cell.meta) asst.appendChild(renderReceipt(cell.meta));

  // Turn actions, revealed on hover/focus (they don't clutter first contact): Edit (grow the
  // cell), Re-run (re-ask), Copy. This is the "reach for it" moment that discloses the spine.
  const actions = el("div", "turn-actions");
  const edit = el("button", "turn-action") as HTMLButtonElement;
  edit.type = "button";
  edit.textContent = "Edit";
  edit.title = "Edit this as a cell";
  edit.addEventListener("click", () => cb.onToggleExpand?.(cell.id, true));
  const rerun = el("button", "turn-action") as HTMLButtonElement;
  rerun.type = "button";
  rerun.textContent = "Re-run";
  rerun.title = "Ask this again (billed)";
  rerun.addEventListener("click", () => cb.onRun?.(cell.id, cell.prompt));
  actions.append(edit, rerun, copyAnswerBtn(cell.answer ?? ""));
  asst.appendChild(actions);

  pair.appendChild(asst);
  wrap.appendChild(pair);
  return wrap;
}

function renderPromptCell(cell: NotebookCell, cb: NotebookCallbacks, showNames: boolean): HTMLElement {
  const wrap = el("div", "notebook-cell" + (cell.stale ? " stale" : ""));
  wrap.dataset.cellId = cell.id;
  wrap.dataset.kind = "prompt";
  wrap.appendChild(renderCellHeader(cell, showNames));

  // Editable prompt — per-cell id (no collision across cells), labelled for a11y.
  const promptId = `nb-prompt-${cell.id}`;
  const label = el("label", "sr-only");
  label.setAttribute("for", promptId);
  label.textContent = "Editable prompt";
  const editor = el("textarea", "notebook-cell-prompt") as HTMLTextAreaElement;
  editor.id = promptId;
  editor.rows = Math.max(2, cell.prompt.split("\n").length);
  editor.value = cell.prompt;
  editor.addEventListener("input", () => cb.onEdit?.(cell.id, editor.value));
  wrap.append(label, editor);

  // Run control (+ a Cancel that collapses back to the chat costume, discarding unrun edits — so
  // revealing the cell is never a one-way trip or a data-loss trap). Offered whenever the user
  // expanded a cell that has a prior answer to fall back to, even if editing made it stale.
  const bar = el("div", "notebook-cell-bar");
  if (cell.expanded && cell.answer) {
    const collapse = el("button", "btn ghost btn-sm notebook-cell-collapse") as HTMLButtonElement;
    collapse.type = "button";
    collapse.textContent = "Cancel";
    collapse.title = "Discard edits and collapse back to the answer";
    collapse.addEventListener("click", () => cb.onToggleExpand?.(cell.id, false));
    bar.appendChild(collapse);
  }
  // Per-cell model pin (#247): a quiet picker so a chain can run one hard step on a stronger model.
  // Default option "" = follow the composer's Model. Left-aligned (auto margin pushes Run right).
  if (cb.onSetModel && cb.modelOptions && cb.modelOptions.length) {
    const modelSel = el("select", "notebook-cell-model") as HTMLSelectElement;
    modelSel.setAttribute("aria-label", "Model for this cell");
    modelSel.title = "Model for this cell (default: follow the composer's Model)";
    const follow = document.createElement("option");
    follow.value = "";
    follow.textContent = "Model: default";
    modelSel.appendChild(follow);
    for (const o of cb.modelOptions) {
      const opt = document.createElement("option");
      opt.value = o.value;
      opt.textContent = o.label;
      modelSel.appendChild(opt);
    }
    modelSel.value = cell.modelId ?? "";
    modelSel.addEventListener("change", () => cb.onSetModel?.(cell.id, modelSel.value || undefined));
    bar.appendChild(modelSel);
  }
  const run = el("button", "btn notebook-cell-run") as HTMLButtonElement;
  run.type = "button";
  // A stale cell with a prior answer says "↻ Re-run" (re-runs the BILLED inference to clear the
  // stale badge); an unrun/fresh cell says "Run". The tooltip keeps "billed" salient so the cost
  // is never a surprise (#247 review — "Refresh" read as free).
  const isRefresh = !!(cell.stale && cell.answer);
  run.textContent = cell.state === "running" ? "Running…" : isRefresh ? "Re-run" : "Run";
  run.title = isRefresh ? "Re-run this cell (billed) — clears the stale badge" : "Run this cell (billed)";
  if (isRefresh) run.classList.add("notebook-cell-refresh");
  run.disabled = cell.state === "running";
  run.addEventListener("click", () => cb.onRun?.(cell.id, editor.value));
  bar.appendChild(run);
  wrap.appendChild(bar);

  // Output: thinking indicator, rendered Markdown answer, or an error.
  const body = el("div", "notebook-answer-body");
  if (cell.state === "running") {
    // Same "Answer" bubble header + Thinking indicator as a chat turn, so an in-progress prompt
    // cell reads identically whether it's wearing the chat or the cell costume (Phase-1 unity).
    const head = el("div", "bubble-head");
    const aBadge = el("div", "bubble-badge");
    aBadge.textContent = "Answer";
    head.appendChild(aBadge);
    body.append(head, thinkingIndicator());
  } else if (cell.state === "error") {
    const err = el("div", "error-msg");
    err.setAttribute("role", "alert");
    err.textContent = `Error: ${cell.error ?? "run failed"}`;
    body.appendChild(err);
  } else if (cell.answer && cell.answer.trim()) {
    // Per-cell citation prefix so [n] anchors don't collide across cells on one page.
    renderInto(body, cell.answer, `${cell.id}-`, {
      onRunCode: cb.onRunFromAnswer ? (code) => cb.onRunFromAnswer?.(cell.id, code) : undefined,
    });
    body.classList.add("rendered");
  }
  wrap.appendChild(body);

  // Sources + receipt + copy (only once answered), reusing the chat helpers.
  if (cell.state === "idle" && cell.answer) {
    if (cell.sources && cell.sources.length) {
      wrap.appendChild(renderSources(cell.sources, `${cell.id}-`));
    }
    if (cell.meta) wrap.appendChild(renderReceipt(cell.meta));
    wrap.appendChild(copyAnswerBtn(cell.answer));
  }
  return wrap;
}

// A code cell: an editable Python source + a Run control that executes it in a client-side
// pyodide worker (#200, slice 2). No server, no network from the cell — stdout / the last
// expression's value / a traceback come back and render below. All output is set via
// textContent (never innerHTML), so nothing here is an XSS sink.
function renderCodeCell(cell: NotebookCell, cb: NotebookCallbacks, showNames: boolean): HTMLElement {
  const wrap = el("div", "notebook-cell notebook-cell-code" + (cell.stale ? " stale" : ""));
  wrap.dataset.cellId = cell.id;
  wrap.dataset.kind = "code";
  wrap.appendChild(renderCellHeader(cell, showNames));

  const sourceId = `nb-code-${cell.id}`;
  const label = el("label", "sr-only");
  label.setAttribute("for", sourceId);
  label.textContent = "Editable code";
  const editor = el("textarea", "notebook-cell-prompt notebook-cell-code-src") as HTMLTextAreaElement;
  editor.id = sourceId;
  editor.spellcheck = false;
  editor.rows = Math.max(3, cell.prompt.split("\n").length);
  editor.value = cell.prompt;
  editor.placeholder = "# Python — runs in your browser";
  editor.addEventListener("input", () => cb.onEdit?.(cell.id, editor.value));
  wrap.append(label, editor);

  const bar = el("div", "notebook-cell-bar");
  const run = el("button", "btn notebook-cell-run") as HTMLButtonElement;
  run.type = "button";
  run.textContent = cell.state === "running" ? "Running…" : "Run";
  run.disabled = cell.state === "running";
  run.addEventListener("click", () => cb.onRunCode?.(cell.id, editor.value));
  const note = el("span", "notebook-code-note");
  note.textContent = "Runs locally in your browser (Python stdlib + numpy/pandas/matplotlib).";
  bar.append(run, note);
  wrap.appendChild(bar);

  // Output pane: loading hint, or captured stdout / value / traceback. textContent only.
  if (cell.state === "running") {
    const loading = el("div", "notebook-code-out notebook-code-loading");
    loading.textContent = cell.error || "Running…"; // error field doubles as a live status
    wrap.appendChild(loading);
  } else if (cell.output) {
    wrap.appendChild(renderCodeOutput(cell.output));
  }
  // A code cell has no billed receipt — show the free-vs-billed distinction inline ($0.00 local).
  if (cell.state === "idle" && cell.output) {
    const receipt = el("div", "notebook-code-receipt");
    receipt.textContent = "$0.00 (local)";
    receipt.title = "Code runs in your browser — no tokens, no cost.";
    wrap.appendChild(receipt);
  }
  return wrap;
}

// An AGENT cell (#248, Canvas move #5): a research question + a budget/time/step CAP the user sets,
// launched as a background agent on AgentCore. It always wears cell chrome (never a chat bubble):
// the cap inputs and the actual-spend-vs-cap receipt must stay visible. The cap is enforced
// server-side by the pre-call cascade — these inputs are the authored envelope, not the guarantee.
// A cap-bounded result (the agent hit its cap) is a normal, honest outcome, badged as partial.
function renderAgentCell(cell: NotebookCell, cb: NotebookCallbacks, showNames: boolean): HTMLElement {
  const wrap = el("div", "notebook-cell notebook-cell-agent" + (cell.stale ? " stale" : ""));
  wrap.dataset.cellId = cell.id;
  wrap.dataset.kind = "agent";
  wrap.appendChild(renderCellHeader(cell, showNames));

  // The research question (editable, like a prompt cell).
  const promptId = `nb-agent-${cell.id}`;
  const label = el("label", "sr-only");
  label.setAttribute("for", promptId);
  label.textContent = "Research question";
  const editor = el("textarea", "notebook-cell-prompt notebook-cell-agent-q") as HTMLTextAreaElement;
  editor.id = promptId;
  editor.rows = Math.max(2, cell.prompt.split("\n").length);
  editor.value = cell.prompt;
  editor.placeholder = "Research question — the agent plans within your cap…";
  editor.addEventListener("input", () => cb.onEdit?.(cell.id, editor.value));
  wrap.append(label, editor);

  // Cap inputs: cost ($), time (min), steps. Each empty = uncapped on that axis. Editing any of
  // them updates the cell's cap (and stales an answered cell — the cap is an input to the result).
  const cap: AgentCap = cell.cap ?? {};
  const capRow = el("div", "notebook-agent-caps");
  const num = (
    key: keyof AgentCap,
    labelText: string,
    displayValue: number | undefined,
    step: string,
    title: string,
    scale = 1, // stored = displayed * scale (minutes → seconds uses 60)
  ): void => {
    const field = el("label", "notebook-agent-cap");
    const span = el("span", "notebook-agent-cap-label");
    span.textContent = labelText;
    const inp = el("input", "notebook-agent-cap-input") as HTMLInputElement;
    inp.type = "number";
    inp.min = "0";
    inp.step = step;
    inp.value = displayValue === undefined ? "" : String(displayValue);
    inp.title = title;
    inp.setAttribute("aria-label", title);
    inp.disabled = cell.state === "running";
    inp.placeholder = "no limit"; // an empty axis is uncapped — say so, don't leave it a mystery
    inp.addEventListener("change", () => {
      const next: AgentCap = { ...(cell.cap ?? {}) };
      const raw = inp.value.trim();
      // Empty OR non-positive → uncapped on this axis. `0` can't mean "spend nothing" (that would
      // never run), so we normalise it to empty AND reflect that back in the field, so the user
      // sees the axis became "no limit" rather than silently believing they set a zero ceiling.
      const n = raw === "" ? NaN : Number(raw);
      if (Number.isFinite(n) && n > 0) next[key] = n * scale;
      else {
        delete next[key];
        inp.value = "";
      }
      cb.onSetCap?.(cell.id, next);
    });
    field.append(span, inp);
    capRow.appendChild(field);
  };
  num("costUsd", "Max $", cap.costUsd, "0.01", "Total dollars this agent may spend (empty = no limit)");
  num("seconds", "Max minutes", cap.seconds ? cap.seconds / 60 : undefined, "0.5", "Wall-clock minutes (empty = no limit)", 60);
  num("maxSteps", "Max steps", cap.maxSteps, "1", "Tool/model calls the agent may make (empty = no limit)");
  wrap.appendChild(capRow);

  // Run control. A stale cell with a prior answer says "↻ Re-run"; otherwise "Run agent". Disabled
  // while running or if no cap axis is set (server refuses an ungoverned launch — mirror it here so
  // the button doesn't offer a launch that will just bounce).
  const bar = el("div", "notebook-cell-bar");
  const hasCap = !!(cap.costUsd || cap.seconds || cap.maxSteps);
  if (cell.state === "running") {
    // A billed background run must be cancellable — offer a real Cancel, not a dead "Running…".
    const cancel = el("button", "btn ghost btn-sm notebook-agent-cancel") as HTMLButtonElement;
    cancel.type = "button";
    cancel.textContent = "Cancel";
    cancel.title = "Stop this run (keeps any partial answer; the agent won't exceed its cap)";
    cancel.addEventListener("click", () => cb.onCancelAgent?.(cell.id));
    bar.appendChild(cancel);
  } else {
    const run = el("button", "btn notebook-cell-run") as HTMLButtonElement;
    run.type = "button";
    const isRefresh = !!(cell.stale && cell.answer);
    run.textContent = isRefresh ? "Re-run agent" : "Run agent";
    run.title = hasCap
      ? "Launch the capped research agent (billed — spends real money up to your cap)"
      : "Set at least one cap (max $, time, or steps) before launching";
    if (isRefresh) run.classList.add("notebook-cell-refresh");
    run.disabled = !hasCap;
    run.addEventListener("click", () => cb.onRunAgent?.(cell.id, editor.value, cell.cap ?? {}));
    bar.appendChild(run);
  }
  // Visible cost framing (not just a tooltip): state plainly that this spends real money up to the
  // cap, and — when Run is disabled for want of a cap — WHY, in text a keyboard/SR user can reach.
  const note = el("span", "notebook-agent-note");
  if (!hasCap) {
    note.textContent = "Set at least one cap (max $, minutes, or steps) to launch.";
    note.classList.add("notebook-agent-note-warn");
  } else {
    const capBits: string[] = [];
    if (cap.costUsd) capBits.push(`$${cap.costUsd.toFixed(2)}`);
    if (cap.seconds) capBits.push(`${(cap.seconds / 60).toFixed(cap.seconds % 60 ? 1 : 0)} min`);
    if (cap.maxSteps) capBits.push(`${cap.maxSteps} steps`);
    // A never-run cell breaks the auto-run expectation Ask/Code set — give an explicit "your move"
    // so the user knows nothing happens until they Run (and what it'll cost). A settled/re-run
    // cell just states the envelope.
    const upTo = `up to ${capBits.join(" / ")} — billed`;
    note.textContent =
      cell.answer || cell.state === "running"
        ? `Runs in the background, spending ${upTo}.`
        : `Review the budget, then Run — spends ${upTo} in the background.`;
  }
  bar.appendChild(note);
  wrap.appendChild(bar);

  // Output: a running indicator, the (possibly partial) answer, or an error.
  const body = el("div", "notebook-answer-body");
  if (cell.state === "running") {
    const head = el("div", "bubble-head");
    const aBadge = el("div", "bubble-badge");
    aBadge.textContent = "Researching";
    head.appendChild(aBadge);
    // Live status (elapsed vs. the time cap, or a step note) — not a black-box spinner.
    if (cell.liveProgress) {
      const live = el("span", "notebook-agent-live");
      live.textContent = cell.liveProgress;
      head.appendChild(live);
    }
    body.append(head, thinkingIndicator());
    // Stream partial answer text as it arrives, so a multi-minute run shows real progress.
    if (cell.answer && cell.answer.trim()) {
      const partial = el("div", "rendered notebook-agent-streaming");
      renderInto(partial, cell.answer, `${cell.id}-`);
      body.appendChild(partial);
    }
  } else if (cell.state === "error") {
    const err = el("div", "error-msg");
    err.setAttribute("role", "alert");
    err.textContent = `Error: ${cell.error ?? "run failed"}`;
    body.appendChild(err);
  } else if (cell.answer && cell.answer.trim()) {
    // A cap-bounded partial answer is flagged so the boundary is honest (success, not error).
    if (cell.agentReceipt?.capBounded) {
      const badge = el("div", "notebook-agent-partial");
      badge.textContent = `Partial — best answer within the cap (${humanizeStopReason(cell.agentReceipt.stopReason)})`;
      badge.title = "The agent returned its best answer within the cap; raise the cap and re-run for more.";
      body.appendChild(badge);
    }
    // Render the answer into its OWN element (renderInto replaces the target's children, so it must
    // not clobber the partial badge appended above).
    const answerBox = el("div", "rendered");
    renderInto(answerBox, cell.answer, `${cell.id}-`, {
      onRunCode: cb.onRunFromAnswer ? (code) => cb.onRunFromAnswer?.(cell.id, code) : undefined,
    });
    body.appendChild(answerBox);
  }
  wrap.appendChild(body);

  // The agent receipt: actual spend / time / steps against the cap (real money — like a billed
  // prompt cell, distinct from a code cell's free line). Shown once the run settles.
  if (cell.state === "idle" && cell.agentReceipt) {
    wrap.appendChild(renderAgentReceipt(cell.agentReceipt, cell.cap));
    if (cell.answer) wrap.appendChild(copyAnswerBtn(cell.answer));
  }
  return wrap;
}

// A short dollar amount for user-facing display: 4 dp (research spends are small but not
// machine-precision), trailing-zero-trimmed so "$0.50" not "$0.5000".
function fmtUsd(n: number): string {
  return `$${Number(n.toFixed(4))}`;
}

// The agent-cell receipt line: spend/time/steps vs. the authored cap. A richer shape than
// renderReceipt (which only knows tokens+cost) — an agent run is measured on three axes. Time is
// shown on the SAME unit as its cap (minutes) so "used vs. budget" is legible at a glance.
function renderAgentReceipt(r: NonNullable<NotebookCell["agentReceipt"]>, cap?: AgentCap): HTMLElement {
  const box = el("div", "notebook-agent-receipt");
  const parts: string[] = [cap?.costUsd ? `${fmtUsd(r.spentUsd)} / ${fmtUsd(cap.costUsd)}` : fmtUsd(r.spentUsd)];
  const usedMin = r.elapsedSeconds / 60;
  const minStr = (m: number) => (m >= 0.1 ? `${m.toFixed(1)} min` : `${r.elapsedSeconds.toFixed(0)}s`);
  parts.push(cap?.seconds ? `${usedMin.toFixed(1)} / ${(cap.seconds / 60).toFixed(1)} min` : minStr(usedMin));
  parts.push(cap?.maxSteps ? `${r.stepsTaken} / ${cap.maxSteps} steps` : `${r.stepsTaken} steps`);
  box.textContent = parts.join(" · ");
  box.title = r.capBounded
    ? `Stopped at the cap: ${humanizeStopReason(r.stopReason)}`
    : "Answered within the cap (spend/time/steps used).";
  return box;
}

// Map a backend stop-reason string to human phrasing (the wire value can be terse, e.g. a raw
// cascade reason). Falls back to the raw string so a new/unknown reason still surfaces something.
function humanizeStopReason(reason: string): string {
  const r = reason.toLowerCase();
  if (r.includes("time")) return "reached the time limit";
  if (r.includes("step")) return "reached the step limit";
  if (r.includes("budget") || r.includes("cost") || r.includes("cap")) return "reached the $ cap";
  return reason;
}

function renderCodeOutput(out: NonNullable<NotebookCell["output"]>): HTMLElement {
  const box = el("div", "notebook-code-out");
  if (out.stdout) {
    const pre = el("pre", "notebook-code-stdout");
    pre.textContent = out.stdout.replace(/\n$/, "");
    box.appendChild(pre);
  }
  if (out.result !== undefined) {
    const pre = el("pre", "notebook-code-result");
    pre.textContent = out.result;
    box.appendChild(pre);
  }
  // matplotlib figures as inline PNGs. The data URI is produced by our own worker; we still
  // hard-validate the `data:image/png;base64,` prefix before assigning src, so a malformed or
  // unexpected value can never become an arbitrary URL.
  for (const img of out.images ?? []) {
    if (typeof img !== "string" || !img.startsWith("data:image/png;base64,")) continue;
    const el2 = document.createElement("img");
    el2.className = "notebook-code-image";
    el2.alt = "figure output";
    el2.src = img;
    box.appendChild(el2);
  }
  if (out.stderr && !out.error) {
    const pre = el("pre", "notebook-code-stderr");
    pre.textContent = out.stderr.replace(/\n$/, "");
    box.appendChild(pre);
  }
  if (out.error) {
    const pre = el("pre", "notebook-code-error");
    pre.setAttribute("role", "alert");
    pre.textContent = out.error.replace(/\n$/, "");
    box.appendChild(pre);
  }
  if (!box.children.length) {
    const empty = el("div", "notebook-code-empty");
    empty.textContent = "(no output)";
    box.appendChild(empty);
  }
  return box;
}
