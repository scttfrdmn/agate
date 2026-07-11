// ChatSession — in-memory conversation over a Transport (design §12 Phase 2:
// "no history persistence yet — in-memory"). Cross-device history (DynamoDB
// on-demand) and AgentCore Memory come later; this keeps the turn loop pure and
// transport-agnostic so swapping tiers never touches the chat logic.

import type { BudgetStatus, ChatMessage, ConverseChunk, Transport } from "../transport";
import { type ContextPolicy, selectContext } from "./context";

// Optional RAG hook: given the user's question, return extra messages (e.g. a
// grounding system message from withContext()) to prepend for this turn only.
// Keeping it a plain function leaves ChatSession decoupled from S3 Vectors.
export type ContextProvider = (query: string) => Promise<ChatMessage[]>;

export interface SendResult {
  text: string;
  usage?: { inputTokens: number; outputTokens: number };
  // This call's cost (USD) and the running period budget, when the transport is
  // the metered choke point. Undefined for browser-direct Bedrock.
  cost?: number;
  budget?: BudgetStatus;
  // The model that actually answered + routing rationale (set by the choke point,
  // especially under "auto" where the server picks). Undefined otherwise.
  model?: string;
  modelRoute?: { model: string; reason: string; degraded: boolean };
}

export interface SendCallbacks {
  onDelta?: (delta: string) => void;
  onReasoning?: (reasoning: string) => void;
}

export class ChatSession {
  private readonly history: ChatMessage[];
  // What of `history` is actually SENT to the model (clear-context floor / sliding window /
  // summary). The full history is still kept for the transcript + memory; this only narrows
  // the wire payload. Managed by the ChatManager so it survives session rebuilds.
  private contextPolicy: ContextPolicy;

  constructor(
    private readonly transport: Transport,
    private readonly modelId: string,
    system?: string,
    private readonly maxTokens?: number,
    // Optional retrieval hook (Phase 3 RAG). When set, each turn is grounded in
    // the user's own in-scope documents; retrieved context is sent for that turn
    // only and never persisted to history (it is re-derived per question).
    private readonly contextProvider?: ContextProvider,
    // Optional external history array to ADOPT (not copy) — the multi-session manager
    // passes the chat's own array so the conversation survives rebuilding the session
    // (e.g. on a model change) and the manager can read the accumulated turns. When
    // omitted, the session keeps its own private history.
    sharedHistory?: ChatMessage[],
    // Optional initial context policy (adopted, then mutable via setContextPolicy). The
    // manager passes the chat's policy so clear/window/summary survive a session rebuild.
    contextPolicy: ContextPolicy = {},
  ) {
    this.history = sharedHistory ?? [];
    if (system && !this.history.some((m) => m.role === "system")) {
      this.history.unshift({ role: "system", content: system });
    }
    this.contextPolicy = contextPolicy;
  }

  /** A copy of the conversation so far (system + turns). */
  get messages(): ChatMessage[] {
    return [...this.history];
  }

  /** The messages that WOULD be sent next turn under the current policy (for the gauge). */
  get sentMessages(): ChatMessage[] {
    return selectContext(this.history, this.contextPolicy);
  }

  /** Update the send policy (clear-context floor / sliding window / summary). */
  setContextPolicy(policy: ContextPolicy): void {
    this.contextPolicy = policy;
  }

  /**
   * Send a user turn and stream the assistant reply. `onDelta` fires for each
   * answer-text chunk and `onReasoning` for any chain-of-thought (reasoning
   * models only). The accumulated answer is appended to history and returned with
   * final usage; reasoning is shown live but never persisted. The assistant turn
   * is committed only once the stream completes, so a failed stream doesn't poison
   * history with a partial.
   */
  async send(
    userText: string,
    callbacks: SendCallbacks = {},
  ): Promise<SendResult> {
    this.history.push({ role: "user", content: userText });

    // Retrieve grounding context for this turn (if RAG is wired). Prepended to
    // the sent messages but NOT stored in history — it's re-derived per question.
    const grounding = this.contextProvider
      ? await this.contextProvider(userText)
      : [];

    let text = "";
    let usage: SendResult["usage"];
    let cost: SendResult["cost"];
    let budget: SendResult["budget"];
    let model: SendResult["model"];
    let modelRoute: SendResult["modelRoute"];
    // Apply the context policy (clear-context floor / sliding window / summary) to decide
    // what history is actually sent — the full history stays for the transcript. Snapshot
    // so the transport never sees a live reference that mutates (we append below).
    for await (const chunk of this.transport.converse({
      modelId: this.modelId,
      messages: [...grounding, ...selectContext(this.history, this.contextPolicy)],
      maxTokens: this.maxTokens,
    })) {
      const c: ConverseChunk = chunk;
      if (c.delta) {
        text += c.delta;
        callbacks.onDelta?.(c.delta);
      }
      if (c.reasoning) {
        // Reasoning is shown live but NOT persisted to history — only the answer
        // is part of the conversation the model sees on the next turn.
        callbacks.onReasoning?.(c.reasoning);
      }
      if (c.usage) usage = c.usage;
      if (c.cost !== undefined) cost = c.cost;
      if (c.budget) budget = c.budget;
      if (c.model) model = c.model;
      if (c.modelRoute) modelRoute = c.modelRoute;
    }

    this.history.push({ role: "assistant", content: text });
    return { text, usage, cost, budget, model, modelRoute };
  }
}
