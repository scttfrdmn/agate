// Notebook persistence (de)serialisation (#200 slice 4) — pure, unit-tested. Converts a live
// Notebook to a plain JSON-safe object for the corpus `_notebooks/` store, and back. Transient
// runtime state (running/error/loading) is dropped on save and reset to "idle" on load; the
// durable content is the cells' kind/name/prompt plus their last computed output (answer +
// receipt for prompt cells, captured output for code cells) so a reopened notebook shows its
// results without a re-run. `stale` is not persisted — a freshly loaded notebook isn't stale.

import type { CellKind, CodeOutput, Notebook, NotebookCell } from "./notebook";
import { newCellId } from "./notebook";
import type { AnswerMeta } from "./ui";
import type { RetrievedChunk } from "../rag/context";

export const NOTEBOOK_SCHEMA = 1;

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
  sources?: RetrievedChunk[];
  meta?: AnswerMeta;
  output?: CodeOutput;
}

/** Serialise a notebook to a JSON-safe object. `name`/`savedAt` are supplied by the caller
 *  (the pure module doesn't read the clock). Drops transient state; keeps computed output. */
export function serializeNotebook(nb: Notebook, name: string, savedAt: string): StoredNotebook {
  return {
    schema: NOTEBOOK_SCHEMA,
    name,
    savedAt,
    cells: nb.cells.map((c) => {
      const s: StoredCell = { kind: c.kind, prompt: c.prompt };
      if (c.name) s.name = c.name;
      if (c.answer !== undefined) s.answer = c.answer;
      if (c.sources) s.sources = c.sources;
      if (c.meta) s.meta = c.meta;
      if (c.output) s.output = c.output;
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
    if (typeof o.answer === "string") cell.answer = o.answer;
    if (Array.isArray(o.sources)) cell.sources = o.sources as unknown as RetrievedChunk[];
    if (isRecord(o.meta)) cell.meta = o.meta as unknown as AnswerMeta;
    if (isRecord(o.output)) cell.output = o.output as unknown as CodeOutput;
    return cell;
  });
  const name = typeof raw.name === "string" ? raw.name : "Untitled notebook";
  return { notebook: { cells }, name };
}
