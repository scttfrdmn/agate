// SPA rendering for the run event stream (§10.2.2, §10.2.12 #1): the multi-pane
// Panel layout and the notebook-style Analyze cell. Framework-free DOM building to
// match the existing plain-TS SPA (web/src/main.ts). The render functions are pure
// in the sense that they take state + a target element and produce DOM; the pure
// reduce()/runStateFrom() in events/collector.ts is what tests exercise.

import type { RunState, PaneState, AnalyzeCell } from "../events/collector";
import type { DivergenceClaim, DivergencePayload } from "../events/protocol";

function el(tag: string, attrs: Record<string, string> = {}, text?: string): HTMLElement {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

// --- Panel: one column per model + a reconciliation column -------------------

export function renderPanel(state: RunState, target: HTMLElement): void {
  target.replaceChildren();
  // group/list semantics so a screen reader announces "Panel, N items".
  const grid = el("div", { class: "agate-panel", role: "group", "aria-label": "Model panel" });
  for (const pane of state.panes) {
    grid.appendChild(renderPane(pane));
  }
  if (state.divergence) {
    grid.appendChild(renderDivergence(state.divergence));
  }
  target.appendChild(grid);
}

function renderPane(pane: PaneState): HTMLElement {
  const done = pane.state === "done";
  const col = el("section", {
    class: `agate-pane ${done ? "done" : "running"}`,
    "data-pane": pane.label,
    "aria-label": `Model ${pane.label}`,
  });
  const head = el("header", {});
  head.appendChild(el("strong", {}, pane.label));
  const status = done
    ? `${pane.elapsed_s ?? "?"}s · $${(pane.cost ?? 0).toFixed(4)}`
    : "…thinking";
  // role=status so the running→done transition is announced.
  head.appendChild(el("span", { class: "pane-status", role: "status" }, status));
  col.appendChild(head);
  col.appendChild(el("div", { class: "agate-pane-body" }, pane.text));
  return col;
}

// --- Divergence: agreement / disagreement / claims-to-verify -----------------

const KIND_LABEL: Record<DivergenceClaim["kind"], string> = {
  agreement: "Agreement",
  disagreement: "Disagreement",
  unsupported: "Unsupported",
};

export function renderDivergence(div: DivergencePayload): HTMLElement {
  const col = el("section", { class: "agate-divergence", "aria-label": "Reconciled view" });
  col.appendChild(el("strong", {}, "Reconciled"));
  col.appendChild(el("p", { class: "cost-line" }, div.summary));

  for (const claim of div.claims) {
    const card = el("details", {
      class: `agate-claim agate-claim-${claim.kind}`,
      "data-verify": String(claim.verify),
    });
    const summary = el("summary", {});
    summary.appendChild(el("span", { class: "agate-claim-kind" }, `${KIND_LABEL[claim.kind]}: `));
    summary.appendChild(document.createTextNode(claim.text));
    if (claim.verify) summary.appendChild(el("span", { title: "verify independently" }, " ⚑"));
    card.appendChild(summary);

    // Conflicting positions side by side.
    const positions = el("ul", { style: "margin:.25rem 0;padding-left:1rem" });
    for (const pos of claim.positions) {
      const note = pos.note ? ` — ${pos.note}` : "";
      positions.appendChild(el("li", {}, `${pos.pane}: ${pos.stance}${note}`));
    }
    card.appendChild(positions);

    if (claim.evidence_refs?.length) {
      card.appendChild(
        el("div", { class: "agate-claim-refs" }, `sources: ${claim.evidence_refs.join(", ")}`),
      );
    }
    col.appendChild(card);
  }
  return col;
}

// --- Analyze: notebook-style editable, re-runnable cell ----------------------

export interface CellCallbacks {
  onRun?: (source: string) => void;
}

export function renderCells(
  cells: AnalyzeCell[],
  target: HTMLElement,
  callbacks: CellCallbacks = {},
): void {
  target.replaceChildren();
  for (const cell of cells) {
    target.appendChild(renderCell(cell, callbacks));
  }
}

function renderCell(cell: AnalyzeCell, callbacks: CellCallbacks): HTMLElement {
  const wrap = el("div", { class: "agate-cell" });

  // Editable source — labelled for assistive tech.
  const label = el("label", { class: "sr-only", for: "agate-cell-source" }, "Editable analysis code");
  wrap.appendChild(label);
  const editor = el("textarea", {
    id: "agate-cell-source",
    class: "agate-cell-source",
    rows: String(Math.max(3, cell.source.split("\n").length)),
    spellcheck: "false",
  }) as HTMLTextAreaElement;
  editor.value = cell.source;
  wrap.appendChild(editor);

  // Re-run control.
  const bar = el("div", { class: "agate-cell-bar" });
  const run = el("button", { class: "btn agate-cell-run", type: "button" }, "Run") as HTMLButtonElement;
  run.addEventListener("click", () => callbacks.onRun?.(editor.value));
  bar.appendChild(run);
  wrap.appendChild(bar);

  // Output: inline chart if present.
  if (cell.chart) {
    const img = el("img", {
      class: "agate-cell-chart",
      alt: "analysis result",
      src: `data:${cell.chart.mime};base64,${cell.chart.data}`,
      style: "max-width:100%;display:block",
    });
    wrap.appendChild(img);
  }
  return wrap;
}
