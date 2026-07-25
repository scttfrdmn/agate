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

import type { CellKind, Notebook, NotebookCell } from "./notebook";
import { referencedNames } from "./dag";
import { copyAnswerBtn, renderReceipt, renderSources } from "./ui";
import { renderInto } from "../render/markdown";

export interface NotebookCallbacks {
  onRun?: (cellId: string, prompt: string) => void;
  onRunCode?: (cellId: string, code: string) => void;
  onAddCell?: (kind: CellKind) => void;
  // The cell's source changed in the editor — used to stale-mark dependents (#200 slice 3).
  onEdit?: (cellId: string, source: string) => void;
  // Reveal ("Edit") or collapse a prompt turn's editable cell chrome — the two-renderer costume
  // change (#242). A no-op for code cells (always shown as cells).
  onToggleExpand?: (cellId: string, expanded: boolean) => void;
  // "Run this" (#243): a Run button on a python block in an answer spawns a code cell seeded with
  // that code, inserted directly below the answering cell (`afterCellId`), then runs it.
  onRunFromAnswer?: (afterCellId: string, code: string) => void;
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

// Whether a prompt cell should render in its editable *cell* costume rather than the read-only
// chat costume. It earns cell chrome when: it has no answer yet (it IS the working cell), the user
// expanded it, it's marked stale (needs an explicit re-run), or another cell references it (its
// {{cN}} output is load-bearing, so its handle + source must be visible).
function showsAsCell(cell: NotebookCell, referenced: Set<string>): boolean {
  if (cell.kind === "code") return true;
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
  const run = el("button", "btn notebook-cell-run") as HTMLButtonElement;
  run.type = "button";
  run.textContent = cell.state === "running" ? "Running…" : "Run";
  run.disabled = cell.state === "running";
  run.addEventListener("click", () => cb.onRun?.(cell.id, editor.value));
  bar.appendChild(run);
  wrap.appendChild(bar);

  // Output: thinking indicator, rendered Markdown answer, or an error.
  const body = el("div", "notebook-answer-body");
  if (cell.state === "running") {
    const thinking = el("div", "thinking");
    const t = el("span", "thinking-label");
    t.textContent = "Thinking";
    const dots = el("span", "thinking-dot");
    dots.innerHTML = "<span></span><span></span><span></span>";
    thinking.append(t, dots);
    body.appendChild(thinking);
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
