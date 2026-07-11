// Notebook cell model + transcript→cells projection (#185, phase 1) — pure, no DOM, no
// transport. A "notebook" is a second VIEW of a chat: the transcript's turns projected
// into a vertical list of editable prompt cells, each re-runnable on its own. This module
// is the pure core (unit-tested); the renderer is notebook-ui.ts and the run path is
// notebook-run.ts. Real marimo (reactive DAG / WASM kernel) is a separate later track —
// the cell model here is deliberately kind-agnostic so a code-cell kind can slot in.

import type { AnswerMeta } from "./ui";
import type { RetrievedChunk } from "../rag/context";
import type { ChatMessage } from "../transport";

// A cell is either a "prompt" cell (an AI turn — billed, routed through the transport) or a
// "code" cell (local computation). Phase-2 slice 1 introduces the discriminator and renders a
// static code cell; the pyodide executor for code cells arrives in a later slice (#200). The
// two kinds stay in one model so a notebook can interleave them.
export type CellKind = "prompt" | "code";

export interface NotebookCell {
  id: string; // stable client id (for DOM keys + per-cell citation namespacing)
  kind: CellKind; // "prompt" (AI turn) or "code" (local computation)
  prompt: string; // the editable source: a question (prompt cell) or code (code cell)
  answer?: string; // the assistant answer, rendered as Markdown (prompt cells; undefined until run)
  sources?: RetrievedChunk[]; // per-cell citations (populated on a run)
  meta?: AnswerMeta; // model / usage / cost (populated on a run)
  state: "idle" | "running" | "error";
  error?: string;
}

export interface Notebook {
  cells: NotebookCell[];
}

// A stable, unguessable cell id — mirrors manager.ts newSessionId (crypto.randomUUID with
// a fallback for non-secure-context/test envs).
export function newCellId(): string {
  const c = globalThis.crypto;
  if (c && "randomUUID" in c) return c.randomUUID();
  return `cell-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
}

/** A fresh, empty (idle, answerless) cell of the given kind — used by "+ Cell". Pure. */
export function newCell(prompt = "", kind: CellKind = "prompt"): NotebookCell {
  return { id: newCellId(), kind, prompt, state: "idle" };
}

/**
 * Project a chat history into notebook cells: each `user` message paired with the
 * following `assistant` message becomes one cell (prompt=user, answer=assistant). Leading
 * `system` messages (RAG grounding / memory seeds) are skipped — they aren't turns. A
 * trailing unpaired `user` message becomes an answerless cell. Pure.
 *
 * Note: `ChatMessage` carries only {role, content} — no per-turn usage/cost/model — so
 * projected cells have `answer` but no `meta`/`sources`; the receipt appears once the cell
 * is re-run (a documented phase-1 limitation).
 */
export function cellsFromHistory(history: ChatMessage[]): NotebookCell[] {
  const cells: NotebookCell[] = [];
  let pending: string | null = null; // an unpaired user prompt awaiting its answer
  for (const msg of history) {
    if (msg.role === "system") continue;
    if (msg.role === "user") {
      if (pending !== null) {
        // Two users in a row (no answer between) — flush the first as answerless.
        cells.push({ id: newCellId(), kind: "prompt", prompt: pending, state: "idle" });
      }
      pending = msg.content;
    } else if (msg.role === "assistant") {
      cells.push({
        id: newCellId(),
        kind: "prompt",
        prompt: pending ?? "",
        answer: msg.content,
        state: "idle",
      });
      pending = null;
    }
  }
  if (pending !== null) {
    cells.push({ id: newCellId(), kind: "prompt", prompt: pending, state: "idle" });
  }
  return cells;
}
