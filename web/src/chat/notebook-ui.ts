// Notebook renderer (#185) — a vertical list of editable prompt cells, each with its
// rendered Markdown answer + a per-cell receipt/Sources footer. Mirrors the editable
// textarea + Run affordance of panes/render.ts renderCells, but renders a Markdown answer
// (via the already-XSS-reviewed renderInto) instead of a code/chart output, reusing the
// exact Sources/receipt/copy markup exported from chat/ui.ts. Framework-free DOM.

import type { CellKind, Notebook, NotebookCell } from "./notebook";
import { copyAnswerBtn, renderReceipt, renderSources } from "./ui";
import { renderInto } from "../render/markdown";

export interface NotebookCallbacks {
  onRun?: (cellId: string, prompt: string) => void;
  onAddCell?: (kind: CellKind) => void;
}

function el(tag: string, cls: string): HTMLElement {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
}

/** Render the whole notebook into `target` (replacing its content). */
export function renderNotebook(
  nb: Notebook,
  target: HTMLElement,
  cb: NotebookCallbacks = {},
): void {
  target.replaceChildren();
  const list = el("div", "notebook");
  for (const cell of nb.cells) list.appendChild(renderCell(cell, cb));
  target.appendChild(list);

  const addBar = el("div", "notebook-add-bar");
  const addPrompt = el("button", "btn ghost notebook-add") as HTMLButtonElement;
  addPrompt.type = "button";
  addPrompt.textContent = "+ Prompt";
  addPrompt.addEventListener("click", () => cb.onAddCell?.("prompt"));
  const addCode = el("button", "btn ghost notebook-add notebook-add-code") as HTMLButtonElement;
  addCode.type = "button";
  addCode.textContent = "+ Code";
  addCode.addEventListener("click", () => cb.onAddCell?.("code"));
  addBar.append(addPrompt, addCode);
  target.appendChild(addBar);
}

function renderCell(cell: NotebookCell, cb: NotebookCallbacks): HTMLElement {
  return cell.kind === "code" ? renderCodeCell(cell) : renderPromptCell(cell, cb);
}

function renderPromptCell(cell: NotebookCell, cb: NotebookCallbacks): HTMLElement {
  const wrap = el("div", "notebook-cell");
  wrap.dataset.cellId = cell.id;
  wrap.dataset.kind = "prompt";

  // Editable prompt — per-cell id (no collision across cells), labelled for a11y.
  const promptId = `nb-prompt-${cell.id}`;
  const label = el("label", "sr-only");
  label.setAttribute("for", promptId);
  label.textContent = "Editable prompt";
  const editor = el("textarea", "notebook-cell-prompt") as HTMLTextAreaElement;
  editor.id = promptId;
  editor.rows = Math.max(2, cell.prompt.split("\n").length);
  editor.value = cell.prompt;
  wrap.append(label, editor);

  // Run control.
  const bar = el("div", "notebook-cell-bar");
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
    renderInto(body, cell.answer, `${cell.id}-`);
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

// A code cell: an editable code source + a (currently disabled) Run control. Execution is a
// later slice (#200) — a lazily-loaded, sandboxed pyodide worker will run the code entirely
// client-side (no server kernel, so NO CLOCKS holds). This slice proves the cell-kind model
// end to end; the editor never executes and never touches the transport, so it's inert.
function renderCodeCell(cell: NotebookCell): HTMLElement {
  const wrap = el("div", "notebook-cell notebook-cell-code");
  wrap.dataset.cellId = cell.id;
  wrap.dataset.kind = "code";

  const sourceId = `nb-code-${cell.id}`;
  const label = el("label", "sr-only");
  label.setAttribute("for", sourceId);
  label.textContent = "Editable code";
  const editor = el("textarea", "notebook-cell-prompt notebook-cell-code-src") as HTMLTextAreaElement;
  editor.id = sourceId;
  editor.spellcheck = false;
  editor.rows = Math.max(3, cell.prompt.split("\n").length);
  editor.value = cell.prompt;
  editor.placeholder = "# Python — runs client-side (coming soon)";
  wrap.append(label, editor);

  // Run is present but disabled until the pyodide executor lands, so the affordance reads as
  // "not yet" rather than missing.
  const bar = el("div", "notebook-cell-bar");
  const run = el("button", "btn notebook-cell-run") as HTMLButtonElement;
  run.type = "button";
  run.textContent = "Run";
  run.disabled = true;
  run.title = "Code execution is coming soon";
  const note = el("span", "notebook-code-note");
  note.textContent = "Local code cells run in your browser — execution ships soon.";
  bar.append(run, note);
  wrap.appendChild(bar);
  return wrap;
}
