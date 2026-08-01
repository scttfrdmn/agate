// Notebook cell run (#185) — a STANDALONE single-prompt metered call, NOT a ChatSession
// turn. Re-running a cell must not pollute the chat transcript's multi-turn history (both
// views back the same ChatRecord), so this mirrors followups.ts: one transport.converse
// with a single user message (plus optional grounding), never a ChatSession. The cost
// folds into the same SessionMeter as a chat turn (wired by the caller).

import type { AgentCap, AgentReceipt } from "./notebook";
import type { ContextProvider } from "./session";
import type { AgentCoreTransport } from "../transport/agentcore";
import type { BudgetStatus, ChatMessage, ConverseChunk, Transport } from "../transport";

export interface CellRunResult {
  text: string;
  usage?: { inputTokens: number; outputTokens: number };
  cost?: number;
  budget?: BudgetStatus;
  model?: string;
  modelRoute?: { model: string; reason: string; degraded: boolean };
}

export interface AgentCellRunResult {
  text: string; // the (possibly partial) answer
  receipt: AgentReceipt; // actual spend/time/steps vs. the cap
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
  images?: string[],
): Promise<CellRunResult> {
  const grounding = contextProvider ? await contextProvider(prompt) : [];
  let text = "";
  let usage: CellRunResult["usage"];
  let cost: CellRunResult["cost"];
  let budget: BudgetStatus | undefined;
  let model: string | undefined;
  let modelRoute: CellRunResult["modelRoute"];
  // Attach any referenced figures (Canvas result→prompt loop, #244) to the user turn so a
  // multimodal model can see them; text-only transports drop them.
  const userMsg: ChatMessage =
    images && images.length ? { role: "user", content: prompt, images } : { role: "user", content: prompt };
  for await (const chunk of transport.converse({
    modelId,
    messages: [...grounding, userMsg],
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

/**
 * Launch an agent cell (#248, Canvas move #5): a budget/time/step-capped background research run on
 * the AgentCore Runtime. Unlike `runCell`, this uses the agent `run()` event stream — the cap is
 * threaded into the invocation payload (snake_case, so the backend routes it to the agent-cell
 * runtime), and the terminal agent-cell receipt event carries the actual spend/time/steps. Answer
 * text arrives as `answer` events (the final one may be titled "partial (cap-bounded)"). `onDelta`
 * streams answer text; `onProgress` surfaces intermediate answer titles (step notes) for a live
 * feel. Returns the settled answer + receipt. The cap itself is ENFORCED server-side — this only
 * carries the authored envelope.
 */
export async function runAgentCell(
  agent: AgentCoreTransport,
  idpToken: string,
  prompt: string,
  cap: AgentCap,
  onDelta?: (delta: string) => void,
  onProgress?: (note: string) => void,
  signal?: AbortSignal,
): Promise<AgentCellRunResult> {
  let text = "";
  // Default receipt: if the run emits no agent-cell receipt (unexpected), report a zero-spend,
  // non-cap-bounded record rather than inventing numbers — the answer still stands.
  let receipt: AgentReceipt = {
    capBounded: false,
    stopReason: "completed",
    spentUsd: 0,
    elapsedSeconds: 0,
    stepsTaken: 0,
  };
  await agent.run(
    {
      question: prompt,
      idp_token: idpToken, // verified server-side; the SPA never sends a tier
      cap: {
        cost_usd: cap.costUsd ?? null,
        seconds: cap.seconds ?? null,
        max_steps: cap.maxSteps ?? null,
      },
    },
    (ev) => {
      if (ev.type === "answer" && ev.text) {
        // A titled answer (e.g. "partial (cap-bounded)") is the final synthesis; an untitled one
        // is streamed answer text. Surface titles as progress; accumulate text either way.
        if (ev.title && onProgress) onProgress(ev.title);
        text += ev.text;
        onDelta?.(ev.text);
      } else if (ev.type === "receipt" && "kind" in ev && ev.kind === "agent-cell") {
        receipt = {
          capBounded: ev.answer_cap_bounded,
          stopReason: ev.stop_reason,
          spentUsd: ev.spent_usd,
          elapsedSeconds: ev.elapsed_seconds,
          stepsTaken: ev.steps_taken,
        };
      }
    },
    undefined,
    signal,
  );
  return { text: text.trim(), receipt };
}
