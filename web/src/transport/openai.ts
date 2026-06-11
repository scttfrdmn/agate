// Tier 1/2 adapter — OpenAI-style fetch against a Lambda Function URL or a
// LiteLLM container (design §2). Opt-in only, for institutions that need exact
// pre-spend cutoffs, centralized inspection, or non-Bedrock routing.
//
// SKELETON (Phase 0). Implemented in Phase 6 if a named requirement forces Tier 1.

import type { Transport, ConverseRequest, ConverseChunk } from "./index";

export class OpenAITransport implements Transport {
  readonly tier = "openai" as const;

  constructor(private readonly endpoint: string) {}

  async *converse(_req: ConverseRequest): AsyncIterable<ConverseChunk> {
    void this.endpoint;
    throw new Error("OpenAITransport.converse not implemented (Tier 1, Phase 6)");
  }
}
