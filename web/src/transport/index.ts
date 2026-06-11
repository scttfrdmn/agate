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

// A streamed token plus optional usage on the final chunk (used by the
// non-authoritative client-side cost estimate — design §7.2).
export interface ConverseChunk {
  delta: string;
  done: boolean;
  usage?: { inputTokens: number; outputTokens: number };
}

export interface Transport {
  readonly tier: "bedrock" | "openai" | "agentcore";
  converse(req: ConverseRequest): AsyncIterable<ConverseChunk>;
}

export type TransportConfig = {
  tier: Transport["tier"];
  region: string;
};
