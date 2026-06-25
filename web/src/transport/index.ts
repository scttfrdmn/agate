// The single client-side transport interface (design §2).
//
// Three implementations satisfy it — bedrock (Tier 0, browser-direct), openai
// (Tier 1/2, hits a Function URL / LiteLLM), and agentcore (agent path). The SPA
// codes against this interface only; switching tiers is a config change, never a
// rewrite. Build the Tier 0 bedrock adapter first (Phase 2); the others are stubs.

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
}

export interface ConverseRequest {
  modelId: string;
  messages: ChatMessage[];
  maxTokens?: number;
}

// The period spend/budget the choke point reports so the UI can show where the
// session stands. budgetUsd is null when no cap is configured. All server-derived.
export interface BudgetStatus {
  period: string;
  spendUsd: number;
  budgetUsd: number | null;
}

// A streamed token plus optional usage on the final chunk (used by the
// non-authoritative client-side cost estimate — design §7.2).
//
// `delta` is answer text. Reasoning models (e.g. gpt-oss) also stream
// chain-of-thought as `reasoning` deltas before the answer; they are surfaced on
// a separate channel so the answer stays clean and the UI can show "thinking…".
// The final chunk may also carry this call's `cost` (USD) and the running
// `budget` status, when the transport is the metered choke point (Tier 1).
export interface ConverseChunk {
  delta: string;
  reasoning?: string;
  done: boolean;
  usage?: { inputTokens: number; outputTokens: number };
  cost?: number;
  budget?: BudgetStatus;
}

export interface Transport {
  readonly tier: "bedrock" | "openai" | "agentcore";
  converse(req: ConverseRequest): AsyncIterable<ConverseChunk>;
}

export type TransportConfig = {
  tier: Transport["tier"];
  region: string;
};
