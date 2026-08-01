// Notebook cell model + transcript→cells projection (#185, phase 1) — pure, no DOM, no
// transport. A "notebook" is a second VIEW of a chat: the transcript's turns projected
// into a vertical list of editable prompt cells, each re-runnable on its own. This module
// is the pure core (unit-tested); the renderer is notebook-ui.ts and the run path is
// notebook-run.ts. Real marimo (reactive DAG / WASM kernel) is a separate later track —
// the cell model here is deliberately kind-agnostic so a code-cell kind can slot in.

import type { AnswerMeta } from "./ui";
import type { RetrievedChunk } from "../rag/context";
import type { ChatMessage } from "../transport";

// A cell is a "prompt" cell (an AI turn — billed, routed through the transport), a "code" cell
// (local Python, run in a client-side pyodide worker, #200), or an "agent" cell (a budget/time/
// step-capped background research agent that runs on AgentCore, #248, Canvas move #5). All three
// kinds share one model so a notebook can interleave them.
export type CellKind = "prompt" | "code" | "agent";

// A user-set cap on an agent cell (Canvas move #5). Any axis may be undefined (uncapped on that
// axis); at least one must be set or a real scope budget must exist, else the launch is refused
// server-side (`agate.agentcell.is_enforceable`). Mirrors the backend `AgentCellCap` — serialised
// to snake_case (`cost_usd`/`seconds`/`max_steps`) in the invocation payload.
export interface AgentCap {
  costUsd?: number; // total dollars the cell may spend across all its steps
  seconds?: number; // wall-clock envelope (soft cap — declines to START a step past it)
  maxSteps?: number; // coarse belt-and-braces bound on tool/model invocations
}

// The frozen receipt of an agent cell's actual run vs. its cap — mirrors the backend
// `research_loop.ResearchResult.receipt` (snake_case on the wire). `capBounded` true means the run
// stopped at a cap and returned a best-effort PARTIAL answer (success, not error, per the design).
export interface AgentReceipt {
  capBounded: boolean;
  stopReason: string;
  spentUsd: number;
  elapsedSeconds: number;
  stepsTaken: number;
}

// Captured output of a code cell run (client-side WASM; stdout / last-expr repr / traceback).
export interface CodeOutput {
  stdout: string;
  stderr: string;
  result?: string; // repr() of the last expression, when the final statement is an expression
  images?: string[]; // matplotlib figures as base64 PNG data URIs (#200 packages)
  error?: string; // Python traceback when the code raised
}

export interface NotebookCell {
  id: string; // stable client id (for DOM keys + per-cell citation namespacing)
  name?: string; // stable short reference name (c1, c2, …) other cells cite via {{cN}} (#200 slice 3)
  kind: CellKind; // "prompt" (AI turn) or "code" (local computation)
  prompt: string; // the editable source: a question (prompt cell) or code (code cell)
  answer?: string; // the assistant answer, rendered as Markdown (prompt cells; undefined until run)
  // The prompt text as it was WHEN `answer` was produced. Lets the surface tell "edited since last
  // run" from "unchanged" so an edited prompt never re-presents a stale answer as authoritative
  // (the freeze/stale discipline). PERSISTED (schema 2, #246): a loaded cell may be stale, so its
  // answeredPrompt can differ from its current prompt; the store reconciles the stale flag from it.
  answeredPrompt?: string;
  // Per-cell model pin (#247/#237): a prompt cell may choose its own entitled model, so a chain can
  // route cheap synthesis and one hard step to a frontier model. Undefined = follow the composer's
  // Model (Auto by default). Persisted so a reopened Canvas re-runs each cell on the same model.
  modelId?: string;
  sources?: RetrievedChunk[]; // per-cell citations (populated on a run)
  meta?: AnswerMeta; // model / usage / cost (populated on a run)
  output?: CodeOutput; // code cells: captured run output (undefined until run)
  // Agent cells (#248): the user-set cap + the frozen receipt of the last run. `cap` is authored
  // before launch and PERSISTED (editing it stales the cell); `agentReceipt` is the actual
  // spend/time/steps vs. the cap, PERSISTED so a reopened Canvas shows the honest boundary.
  cap?: AgentCap;
  agentReceipt?: AgentReceipt;
  // Transient live status for a RUNNING agent cell (elapsed time / step notes) — a black-box
  // multi-minute spinner is unacceptable for a billed background run. Not persisted (run state).
  liveProgress?: string;
  state: "idle" | "running" | "error";
  // A cell whose upstream reference changed since its last run. Code cells auto-re-run to clear
  // it (free); prompt (AI) cells stay stale until an explicit, billed re-run (#200 slice 3).
  stale?: boolean;
  error?: string;
  // Two renderers, one model (Canvas #242): an answered prompt cell renders as a read-only chat
  // turn (question + answer + one receipt) UNTIL the user reaches for more — editing it, or
  // another cell referencing it — at which point it shows its editable cell chrome. `expanded`
  // is that user-driven "show me the cell" flag. Code cells always render as cells (this is
  // ignored for them). Transient UI state — not persisted.
  expanded?: boolean;
  // For a code cell spawned by "Run this" (#243): the id of the answer cell its code came from, so
  // several blocks Run from the same answer keep their order (each inserts after the previous one).
  // Transient — not persisted.
  spawnedFrom?: string;
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

/**
 * Whether an answered prompt cell's current source no longer matches the prompt that produced its
 * answer — i.e. it was edited since its last run and its frozen answer is now stale. A cell with no
 * answer yet is never "stale" in this sense (it has nothing to contradict). Pure. Used to fence the
 * two-renderer surface: a mismatched cell must stay an editable cell, never a chat turn presenting
 * an answer that doesn't correspond to the shown question.
 */
export function isEditedSinceRun(cell: NotebookCell): boolean {
  return cell.answer !== undefined && cell.prompt !== cell.answeredPrompt;
}

/** A fresh, empty (idle, answerless) cell of the given kind — used by "+ Cell". The optional
 *  `name` is the {{cN}} reference handle (assigned by the caller, which knows the notebook).
 *  `cap` seeds an agent cell's budget/time/step envelope (ignored for other kinds). Pure. */
export function newCell(
  prompt = "",
  kind: CellKind = "prompt",
  name?: string,
  cap?: AgentCap,
): NotebookCell {
  const cell: NotebookCell = { id: newCellId(), name, kind, prompt, state: "idle" };
  if (kind === "agent" && cap) cell.cap = cap;
  return cell;
}

/**
 * Project a chat history into notebook cells: each `user` message paired with the
 * following `assistant` message becomes one cell (prompt=user, answer=assistant). Leading
 * `system` messages (RAG grounding / memory seeds) are skipped — they aren't turns. A
 * trailing unpaired `user` message becomes an answerless cell. Pure.
 *
 * `ChatMessage` carries only {role, content}, so per-turn usage/cost/model comes in via the
 * optional `turnMeta` list (#245): one entry per ANSWERED turn, in transcript order, captured by
 * the ChatManager as each turn finishes (kept 1:1 with the assistant messages in `history`). When
 * present, each answered cell gets its own receipt (tokens in/out + cost) — the standing per-cell
 * cost line move #3a wants — not just re-run cells. Without it, cells still have `answer` and the
 * receipt appears once the cell is re-run. (A notebook OPENED from disk doesn't use this path — it
 * deserializes cells with their `meta` intact, so its receipts show immediately.)
 */
export function cellsFromHistory(history: ChatMessage[], turnMeta?: AnswerMeta[]): NotebookCell[] {
  const cells: NotebookCell[] = [];
  let pending: string | null = null; // an unpaired user prompt awaiting its answer
  let answered = 0; // count of answered turns so far → index into turnMeta (transcript order)
  for (const msg of history) {
    if (msg.role === "system") continue;
    if (msg.role === "user") {
      if (pending !== null) {
        // Two users in a row (no answer between) — flush the first as answerless.
        cells.push({ id: newCellId(), kind: "prompt", prompt: pending, state: "idle" });
      }
      pending = msg.content;
    } else if (msg.role === "assistant") {
      const meta = turnMeta?.[answered];
      answered += 1;
      cells.push({
        id: newCellId(),
        kind: "prompt",
        prompt: pending ?? "",
        answer: msg.content,
        answeredPrompt: pending ?? "", // the answer corresponds to this prompt as loaded
        ...(meta ? { meta } : {}),
        state: "idle",
      });
      pending = null;
    }
  }
  if (pending !== null) {
    cells.push({ id: newCellId(), kind: "prompt", prompt: pending, state: "idle" });
  }
  // Assign stable {{cN}} reference names (#200 slice 3), 1-based in transcript order.
  cells.forEach((c, i) => (c.name = `c${i + 1}`));
  return cells;
}
