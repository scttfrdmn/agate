// Notebook persistence (de)serialisation (#200 slice 4; #246 freeze/stale under version control) —
// pure, unit-tested. Converts a live Notebook to a JSON-safe, git-diff-friendly object for the
// corpus `_notebooks/` store, and back.
//
// The durable content is each cell's kind/name/prompt + its last COMPUTED output (answer + receipt
// for prompt cells, captured output for code cells) so a reopened Canvas shows its frozen results
// with no re-inference and no cost (the reproducibility guarantee). Transient run state
// (running/error/loading) is dropped and reset to "idle" on load.
//
// Freeze/stale (#246): a prompt cell's frozen output travels with the cell, and its `stale` flag +
// the `answeredPrompt` it was produced from ARE persisted — so a committed Canvas reopens with its
// stale cells still badged (stale is a flag, not a trigger; it never auto-re-runs). This is the
// state machine in docs/agate-canvas.md, extended to persistence.
//
// Diff-stability: the volatile per-cell `id` is intentionally NOT serialized (fresh ids on load),
// so a `git diff` reflects real content changes, not id churn.

import type { CellKind, CodeOutput, Notebook, NotebookCell } from "./notebook";
import { newCellId } from "./notebook";
import type { AnswerMeta } from "./ui";
import type { RetrievedChunk } from "../rag/context";

// Schema 2 (#246): adds persisted `stale` + `answeredPrompt`. v1 files still load (a v1 cell had no
// stale/answeredPrompt → treated as fresh, answeredPrompt defaults to its prompt).
export const NOTEBOOK_SCHEMA = 2;

export interface StoredNotebook {
  schema: number;
  name: string;
  savedAt: string; // ISO timestamp, stamped by the caller (no clocks in this pure module)
  cells: StoredCell[];
}

interface StoredCell {
  name?: string;
  kind: CellKind;
  prompt: string;
  answer?: string;
  // The prompt text that produced `answer` — persisted so the frozen/stale relationship survives a
  // round-trip (a cell edited-but-not-rerun stays stale on reopen instead of falsely re-matching).
  answeredPrompt?: string;
  sources?: RetrievedChunk[];
  meta?: AnswerMeta;
  output?: CodeOutput;
  // A cell that was stale when saved reopens stale-badged (freeze/stale under version control).
  stale?: boolean;
}

/** Serialise a notebook to a JSON-safe object. `name`/`savedAt` are supplied by the caller
 *  (the pure module doesn't read the clock). Drops transient run state; keeps the frozen output
 *  plus the freeze/stale bookkeeping (#246). */
export function serializeNotebook(nb: Notebook, name: string, savedAt: string): StoredNotebook {
  return {
    schema: NOTEBOOK_SCHEMA,
    name,
    savedAt,
    cells: nb.cells.map((c) => {
      const s: StoredCell = { kind: c.kind, prompt: c.prompt };
      if (c.name) s.name = c.name;
      if (c.answer !== undefined) s.answer = c.answer;
      if (c.answeredPrompt !== undefined) s.answeredPrompt = c.answeredPrompt;
      if (c.sources) s.sources = c.sources;
      if (c.meta) s.meta = c.meta;
      if (c.output) s.output = c.output; // includes any captured figure PNGs
      if (c.stale) s.stale = true;
      return s;
    }),
  };
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

/** Parse a stored object back into a Notebook. Tolerant of unknown/missing fields: a cell with
 *  no valid kind defaults to "prompt"; every cell gets a fresh id and idle state. Returns null
 *  if the payload isn't a recognisable notebook (unknown schema or no cells array). */
export function deserializeNotebook(raw: unknown): { notebook: Notebook; name: string } | null {
  if (!isRecord(raw) || !Array.isArray(raw.cells)) return null;
  if (typeof raw.schema === "number" && raw.schema > NOTEBOOK_SCHEMA) return null; // newer than we know
  const cells: NotebookCell[] = raw.cells.map((c) => {
    const o = isRecord(c) ? c : {};
    const kind: CellKind = o.kind === "code" ? "code" : "prompt";
    const cell: NotebookCell = {
      id: newCellId(),
      kind,
      prompt: typeof o.prompt === "string" ? o.prompt : "",
      state: "idle",
    };
    if (typeof o.name === "string") cell.name = o.name;
    if (typeof o.answer === "string") {
      cell.answer = o.answer;
      // Restore the frozen/stale relationship (#246): use the persisted answeredPrompt (schema 2);
      // for a v1 file (no answeredPrompt) fall back to the saved prompt, i.e. treat as matched.
      cell.answeredPrompt = typeof o.answeredPrompt === "string" ? o.answeredPrompt : cell.prompt;
    }
    if (Array.isArray(o.sources)) cell.sources = o.sources as unknown as RetrievedChunk[];
    if (isRecord(o.meta)) cell.meta = o.meta as unknown as AnswerMeta;
    if (isRecord(o.output)) cell.output = o.output as unknown as CodeOutput;
    // Reopen stale-badged — a flag, never a trigger (never auto-re-runs). We RECONCILE rather than
    // trust the persisted bit alone: a cell with a frozen value (answer or code output) is stale if
    // it was saved stale OR its prompt no longer matches the prompt that produced the answer. That
    // second clause matters for a hand-edited / git-merged file where `stale` might be absent but
    // prompt≠answeredPrompt — without it a mismatched Q&A would reopen as an authoritative chat
    // turn (showsAsCell and isEditedSinceRun would disagree). Keys on answer OR output so a stale
    // code cell (which has output, not answer) also survives.
    const hasFrozenValue = cell.answer !== undefined || cell.output !== undefined;
    const editedSinceRun = cell.answer !== undefined && cell.answeredPrompt !== cell.prompt;
    if (hasFrozenValue && (o.stale === true || editedSinceRun)) cell.stale = true;
    return cell;
  });
  const name = typeof raw.name === "string" ? raw.name : "Untitled notebook";
  return { notebook: { cells }, name };
}
