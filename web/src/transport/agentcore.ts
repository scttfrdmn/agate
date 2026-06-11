// Agent-path adapter — invoke an AgentCore Runtime agent (design §2, §13.7).
// For genuinely agentic work (tools, code, multi-model). Never on the plain-chat
// path — a single Converse stays browser-direct (Tier 0).
//
// SKELETON (Phase 0). Implemented in Phase 8.

import type { Transport, ConverseRequest, ConverseChunk } from "./index";

export class AgentCoreTransport implements Transport {
  readonly tier = "agentcore" as const;

  constructor(private readonly runtimeArn: string) {}

  async *converse(_req: ConverseRequest): AsyncIterable<ConverseChunk> {
    void this.runtimeArn;
    throw new Error("AgentCoreTransport.converse not implemented (agent path, Phase 8)");
  }
}
