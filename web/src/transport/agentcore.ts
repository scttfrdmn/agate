// Agent-path adapter — invoke an AgentCore Runtime agent (design §2, §13.7).
//
// For genuinely agentic work (Panel, Analyze) — never plain chat (a single
// Converse stays browser-direct, Tier 0). The Runtime returns the run as an
// event stream; this adapter decodes it into the shared RunEvent protocol
// (§10.2.9). SigV4-signed with the broker-vended scoped credentials.

import {
  BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
} from "@aws-sdk/client-bedrock-agentcore";

import type { ScopedCredentials } from "../auth";
import { toSdkCredentials as sdkCreds } from "../auth/sdkCreds";
import type { Emit, RunEvent } from "../events/protocol";
import type { ConverseChunk, ConverseRequest, Transport } from "./index";

export interface AgentCoreConfig {
  region: string;
  // The deployed Runtime ARN (agate-agent stack output).
  runtimeArn: string;
  qualifier?: string; // endpoint qualifier, defaults to the "default" endpoint
}

// The invocation payload the reference agent (agent/server.py) expects.
export interface AgentInvocation {
  question: string;
  // The campus-IdP token the container verifies server-side to derive the caller's
  // tier/tenant (SEC-4b) — never trust a payload tier field. Required for a real run.
  idp_token?: string;
  mode?: "SYNTHESIS" | "DEBATE" | "ANALYSIS";
  evidence?: string;
  roster?: Array<Record<string, unknown>>;
  adjudicator?: Record<string, unknown>;
  router?: Record<string, unknown>;
  generator?: Record<string, unknown>;
  code?: string;
}


// Parse the agent's newline-delimited-JSON response blob into RunEvents. Pure and
// exported for testing. Blank lines and unparseable lines are skipped (robust to a
// trailing newline); the SPA's reducer tolerates unknown event types.
export function parseEventBlob(blob: string): RunEvent[] {
  const events: RunEvent[] = [];
  for (const line of blob.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      events.push(JSON.parse(trimmed) as RunEvent);
    } catch {
      // skip a malformed line rather than failing the whole run
    }
  }
  return events;
}

export class AgentCoreTransport implements Transport {
  readonly tier = "agentcore" as const;

  constructor(
    private readonly cfg: AgentCoreConfig,
    private readonly creds: () => Promise<ScopedCredentials>,
  ) {}

  private async client(): Promise<BedrockAgentCoreClient> {
    return new BedrockAgentCoreClient({
      region: this.cfg.region,
      credentials: async () => sdkCreds(await this.creds()),
    });
  }

  // The agent-path entry point: invoke the Runtime and emit the decoded RunEvent
  // stream. `sessionId` ties a multi-turn run to one microVM session (≤ the
  // session idle timeout); omit for a fresh session.
  async run(invocation: AgentInvocation, emit: Emit, sessionId?: string): Promise<void> {
    const client = await this.client();
    const payload = new TextEncoder().encode(JSON.stringify(invocation));
    const res = await client.send(
      new InvokeAgentRuntimeCommand({
        agentRuntimeArn: this.cfg.runtimeArn,
        qualifier: this.cfg.qualifier,
        runtimeSessionId: sessionId,
        contentType: "application/json",
        accept: "application/x-ndjson",
        payload,
      }),
    );
    // The response blob is a streaming payload in the browser SDK; collect it.
    const blob = res.response ? await res.response.transformToString() : "";
    for (const event of parseEventBlob(blob)) emit(event);
  }

  // Transport-interface conformance: adapt a plain Converse request to an agent
  // invocation and surface only the answer text as chunks. The richer event stream
  // (panes, divergence, code, charts) is available via run(); converse() is the
  // lowest-common-denominator path so the SPA can treat all tiers uniformly.
  async *converse(req: ConverseRequest): AsyncIterable<ConverseChunk> {
    const question = req.messages.filter((m) => m.role === "user").at(-1)?.content ?? "";
    const events: RunEvent[] = [];
    await this.run({ question, generator: { tier: req.modelId, label: req.modelId, max_tokens: req.maxTokens ?? 1024 } }, (e) => events.push(e));
    for (const e of events) {
      if (e.type === "answer" && e.text) yield { delta: e.text, done: false };
    }
    yield { delta: "", done: true };
  }
}
