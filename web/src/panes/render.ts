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
  const grid = el("div", {
    class: "agg-panel",
    style: "display:grid;grid-auto-flow:column;gap:1rem;align-items:start",
  });
  for (const pane of state.panes) {
    grid.appendChild(renderPane(pane));
  }
  if (state.divergence) {
    grid.appendChild(renderDivergence(state.divergence));
  }
  target.appendChild(grid);
}

function renderPane(pane: PaneState): HTMLElement {
  const col = el("section", {
    class: "agg-pane",
    "data-pane": pane.label,
    style: "border:1px solid #ddd;padding:.75rem;min-width:18rem",
  });
  const head = el("header", { style: "display:flex;justify-content:space-between;gap:.5rem" });
  head.appendChild(el("strong", {}, pane.label));
  const status =
    pane.state === "done"
      ? `${pane.elapsed_s ?? "?"}s · $${(pane.cost ?? 0).toFixed(4)}`
      : "…thinking";
  head.appendChild(el("span", { style: "color:#666;font-size:.85em" }, status));
  col.appendChild(head);
  col.appendChild(el("div", { class: "agg-pane-body", style: "white-space:pre-wrap;margin-top:.5rem" }, pane.text));
  return col;
}

// --- Divergence: agreement / disagreement / claims-to-verify -----------------

const KIND_LABEL: Record<DivergenceClaim["kind"], string> = {
  agreement: "Agreement",
  disagreement: "Disagreement",
  unsupported: "Unsupported",
};

export function renderDivergence(div: DivergencePayload): HTMLElement {
  const col = el("section", {
    class: "agg-divergence",
    style: "border:1px solid #bbb;background:#fafafa;padding:.75rem;min-width:20rem",
  });
  col.appendChild(el("strong", {}, "Reconciled"));
  col.appendChild(el("p", { style: "color:#444" }, div.summary));

  for (const claim of div.claims) {
    const card = el("details", {
      class: `agg-claim agg-claim-${claim.kind}`,
      "data-verify": String(claim.verify),
      style: "margin:.5rem 0;border-left:3px solid #ccc;padding-left:.5rem",
    });
    const summary = el("summary", {});
    summary.appendChild(el("span", { class: "agg-claim-kind" }, `${KIND_LABEL[claim.kind]}: `));
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
        el("div", { class: "agg-claim-refs", style: "font-size:.8em;color:#777" },
          `sources: ${claim.evidence_refs.join(", ")}`),
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
  const wrap = el("div", { class: "agg-cell", style: "border:1px solid #ddd;margin:.5rem 0" });

  // Editable source.
  const editor = el("textarea", {
    class: "agg-cell-source",
    rows: String(Math.max(3, cell.source.split("\n").length)),
    style: "width:100%;font-family:ui-monospace,monospace;border:0;padding:.5rem",
  }) as HTMLTextAreaElement;
  editor.value = cell.source;
  wrap.appendChild(editor);

  // Re-run control.
  const bar = el("div", { style: "display:flex;justify-content:flex-end;padding:.25rem;background:#f6f6f6" });
  const run = el("button", { class: "agg-cell-run" }, "Run") as HTMLButtonElement;
  run.addEventListener("click", () => callbacks.onRun?.(editor.value));
  bar.appendChild(run);
  wrap.appendChild(bar);

  // Output: inline chart if present.
  if (cell.chart) {
    const img = el("img", {
      class: "agg-cell-chart",
      alt: "analysis result",
      src: `data:${cell.chart.mime};base64,${cell.chart.data}`,
      style: "max-width:100%;display:block",
    });
    wrap.appendChild(img);
  }
  return wrap;
}
