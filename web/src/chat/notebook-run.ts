// Notebook cell run (#185) — a STANDALONE single-prompt metered call, NOT a ChatSession
// turn. Re-running a cell must not pollute the chat transcript's multi-turn history (both
// views back the same ChatRecord), so this mirrors followups.ts: one transport.converse
// with a single user message (plus optional grounding), never a ChatSession. The cost
// folds into the same SessionMeter as a chat turn (wired by the caller).

import type { ContextProvider } from "./session";
import type { BudgetStatus, ConverseChunk, Transport } from "../transport";

export interface CellRunResult {
  text: string;
  usage?: { inputTokens: number; outputTokens: number };
  cost?: number;
  budget?: BudgetStatus;
  model?: string;
  modelRoute?: { model: string; reason: string; degraded: boolean };
}

/**
 * Run one prompt as a standalone metered call. `modelId` may be a pin or the literal
 * "auto" (the server routes, #190). When `contextProvider` is set (RAG / memory), its
 * messages are prepended for this call only. `onDelta` streams answer text live. Never
 * touches a ChatSession or any history array — a cell is self-contained.
 */
export async function runCell(
  transport: Transport,
  modelId: string,
  prompt: string,
  contextProvider?: ContextProvider,
  onDelta?: (delta: string) => void,
): Promise<CellRunResult> {
  const grounding = contextProvider ? await contextProvider(prompt) : [];
  let text = "";
  let usage: CellRunResult["usage"];
  let cost: CellRunResult["cost"];
  let budget: BudgetStatus | undefined;
  let model: string | undefined;
  let modelRoute: CellRunResult["modelRoute"];
  for await (const chunk of transport.converse({
    modelId,
    messages: [...grounding, { role: "user", content: prompt }],
  })) {
    const c: ConverseChunk = chunk;
    if (c.delta) {
      text += c.delta;
      onDelta?.(c.delta);
    }
    if (c.usage) usage = c.usage;
    if (c.cost !== undefined) cost = c.cost;
    if (c.budget) budget = c.budget;
    if (c.model) model = c.model;
    if (c.modelRoute) modelRoute = c.modelRoute;
  }
  return { text, usage, cost, budget, model, modelRoute };
}
