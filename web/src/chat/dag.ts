// Cross-cell dependency graph for notebook cells (#200, phase 2 slice 3) — the "cost-aware
// reactive notebook" core. A cell references another by its stable name with a {{cN}} token:
//   • prompt cell: {{c1}} is replaced by c1's output text (e.g. "summarize {{c1}}")
//   • code cell:   {{c1}} is replaced by a JSON-encoded Python string literal (x = {{c1}})
// Resolution is pure textual pre-processing (no kernel/transport change). On a change, code
// dependents re-run automatically (free, local WASM) while AI/prompt dependents are only
// marked stale for an explicit, billed re-run — so reactivity never spends tokens silently.
//
// Everything here is pure and unit-tested; the UI (notebook-ui.ts) and run wiring (main.ts)
// consume it. Cycles are handled defensively (visited-set), so a mutual {{}} reference can't
// loop forever — it just stops propagating.

import type { NotebookCell } from "./notebook";

// A {{name}} reference: a leading letter then word chars, optional inner whitespace.
export const REF_RE = /\{\{\s*([A-Za-z][\w]*)\s*\}\}/g;

/** Names referenced by `source` that exist in `known`, de-duplicated, in first-seen order. */
export function refsIn(source: string, known: Set<string>): string[] {
  const out: string[] = [];
  for (const m of source.matchAll(REF_RE)) {
    const name = m[1];
    if (known.has(name) && !out.includes(name)) out.push(name);
  }
  return out;
}

/** The text another cell exposes when referenced: a code cell's value/stdout, or a prompt
 *  cell's answer. Empty string when the cell hasn't produced output yet. Pure. */
export function outputText(cell: NotebookCell): string {
  if (cell.kind === "code") {
    return cell.output?.result ?? cell.output?.stdout?.replace(/\n$/, "") ?? "";
  }
  return cell.answer ?? "";
}

function byName(cells: NotebookCell[]): Map<string, NotebookCell> {
  const m = new Map<string, NotebookCell>();
  for (const c of cells) if (c.name) m.set(c.name, c);
  return m;
}

/**
 * Substitute {{name}} references in a cell's source with the referenced cells' output.
 * For a code cell the referenced text is JSON-encoded (a valid Python string literal); for a
 * prompt cell it's inlined raw. Unknown names are left untouched. Returns the resolved source
 * plus the resolved dependency names. Pure.
 */
export function resolveSource(
  cell: NotebookCell,
  cells: NotebookCell[],
): { resolved: string; deps: string[] } {
  const named = byName(cells);
  const deps: string[] = [];
  const resolved = cell.prompt.replace(REF_RE, (whole, name: string) => {
    const ref = named.get(name);
    if (!ref || ref.id === cell.id) return whole; // unknown or self-ref: leave as-is
    if (!deps.includes(name)) deps.push(name);
    const text = outputText(ref);
    return cell.kind === "code" ? JSON.stringify(text) : text;
  });
  return { resolved, deps };
}

/** id → ids it directly depends on (the cells it references). Pure. */
export function buildDeps(cells: NotebookCell[]): Map<string, string[]> {
  const named = byName(cells);
  const known = new Set(named.keys());
  const deps = new Map<string, string[]>();
  for (const c of cells) {
    const ids: string[] = [];
    for (const name of refsIn(c.prompt, known)) {
      const ref = named.get(name);
      if (ref && ref.id !== c.id) ids.push(ref.id);
    }
    deps.set(c.id, ids);
  }
  return deps;
}

/**
 * Transitive dependents of `changedId` — the cells that (directly or indirectly) reference it —
 * in topological order (a cell appears after every dependent it in turn depends on), so an
 * auto-re-run cascade runs upstream-before-downstream. Excludes `changedId` itself. Cycle-safe.
 */
export function dependentsOf(cells: NotebookCell[], changedId: string): NotebookCell[] {
  const deps = buildDeps(cells);
  // Reverse edges: id → ids that depend on it.
  const dependents = new Map<string, string[]>();
  for (const [id, ds] of deps) {
    for (const d of ds) {
      const arr = dependents.get(d) ?? [];
      arr.push(id);
      dependents.set(d, arr);
    }
  }
  // Collect the transitive dependent set (BFS from changedId over reverse edges).
  const affected = new Set<string>();
  const queue = [...(dependents.get(changedId) ?? [])];
  while (queue.length) {
    const id = queue.shift()!;
    if (affected.has(id)) continue;
    affected.add(id);
    for (const next of dependents.get(id) ?? []) if (!affected.has(next)) queue.push(next);
  }
  // Topologically order the affected subgraph: DFS post-order over dependencies within the set.
  const byId = new Map(cells.map((c) => [c.id, c]));
  const order: string[] = [];
  const seen = new Set<string>();
  const visiting = new Set<string>();
  const visit = (id: string): void => {
    if (seen.has(id) || visiting.has(id)) return; // visiting-guard breaks cycles
    visiting.add(id);
    for (const dep of deps.get(id) ?? []) if (affected.has(dep)) visit(dep);
    visiting.delete(id);
    seen.add(id);
    order.push(id);
  };
  for (const id of affected) visit(id);
  return order.map((id) => byId.get(id)!).filter(Boolean);
}

/** The next free `cN` name for a notebook (max existing number + 1). Pure. */
export function nextCellName(cells: NotebookCell[]): string {
  let max = 0;
  for (const c of cells) {
    const m = /^c(\d+)$/.exec(c.name ?? "");
    if (m) max = Math.max(max, Number(m[1]));
  }
  return `c${max + 1}`;
}
