// Tier 0 adapter — browser-direct Bedrock Converse with SigV4, using the
// short-lived scoped credentials from the Cognito identity exchange (design §2).
//
// SKELETON (Phase 0). Phase 2 wires the streaming ConverseStream call against
// BedrockRuntimeClient. The credentials are the user's own, narrowed by agg:
// session tags — this adapter holds NO long-lived secret.

import type { Transport, ConverseRequest, ConverseChunk } from "./index";
import type { ScopedCredentials } from "../auth";

export class BedrockTransport implements Transport {
  readonly tier = "bedrock" as const;

  constructor(
    private readonly region: string,
    private readonly creds: () => Promise<ScopedCredentials>,
  ) {}

  async *converse(_req: ConverseRequest): AsyncIterable<ConverseChunk> {
    // Referenced so the Phase 0 skeleton typechecks under strict unused checks;
    // Phase 2 uses both to build the SigV4-signed ConverseStream call.
    void this.region;
    void this.creds;
    throw new Error("BedrockTransport.converse not implemented until Phase 2");
  }
}
