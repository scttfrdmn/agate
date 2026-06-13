// Tier 1/2 adapter — OpenAI-style fetch against the Tier 1 choke-point Lambda
// Function URL (design §2, §12 Phase 6). Opt-in only, for institutions that need
// exact pre-spend cutoffs, centralized inspection, or non-Bedrock routing. The
// Function URL is AWS_IAM-authed, so the request is SigV4-signed with the
// broker-vended scoped credentials — same identity boundary as Tier 0.

import { SignatureV4 } from "@smithy/signature-v4";
import { Sha256 } from "@aws-crypto/sha256-js";

import type { ScopedCredentials } from "../auth";
import type { ChatMessage, ConverseChunk, ConverseRequest, Transport } from "./index";

export interface OpenAIConfig {
  region: string;
  endpoint: string; // the chokepoint Function URL
  // Scope the request carries so the Lambda can read spend + assume the user role.
  scope: () => { tenant: string; user: string; period: string; tier: string; courses: string[]; budget?: number };
}

function sdkCreds(c: ScopedCredentials) {
  return {
    accessKeyId: c.accessKeyId,
    secretAccessKey: c.secretAccessKey,
    sessionToken: c.sessionToken,
  };
}

// Pure: build the Tier 1 request body from a ConverseRequest + session scope.
// Exported for testing without the network.
export function buildRequestBody(
  req: ConverseRequest,
  scope: ReturnType<OpenAIConfig["scope"]>,
): Record<string, unknown> {
  const messages = req.messages.map((m: ChatMessage) => ({ role: m.role, content: m.content }));
  return {
    model: req.modelId,
    messages,
    max_tokens: req.maxTokens ?? 1024,
    tenant: scope.tenant,
    user: scope.user,
    period: scope.period,
    tier: scope.tier,
    courses: scope.courses,
    budget: scope.budget,
  };
}

// Pure: map the choke-point JSON response (or a budget rejection) to chunks.
// The Lambda returns {text, usage} on allow, or a 402 {error,detail} on reject.
export function responseToChunks(status: number, payload: Record<string, unknown>): ConverseChunk[] {
  if (status === 402) {
    // Budget rejection — surface it as answer text rather than a silent failure.
    const detail = typeof payload.detail === "string" ? payload.detail : "budget rejected";
    return [{ delta: `[budget] ${detail}`, done: true }];
  }
  if (status !== 200) {
    return [{ delta: `[error] ${String(payload.error ?? status)}`, done: true }];
  }
  const text = typeof payload.text === "string" ? payload.text : "";
  const usage = (payload.usage ?? {}) as { inputTokens?: number; outputTokens?: number };
  return [
    { delta: text, done: false },
    {
      delta: "",
      done: true,
      usage: { inputTokens: usage.inputTokens ?? 0, outputTokens: usage.outputTokens ?? 0 },
    },
  ];
}

export class OpenAITransport implements Transport {
  readonly tier = "openai" as const;

  constructor(
    private readonly cfg: OpenAIConfig,
    private readonly creds: () => Promise<ScopedCredentials>,
  ) {}

  async *converse(req: ConverseRequest): AsyncIterable<ConverseChunk> {
    const body = JSON.stringify(buildRequestBody(req, this.cfg.scope()));
    const url = new URL(this.cfg.endpoint);

    // SigV4-sign the POST (the Function URL is AWS_IAM-authed).
    const signer = new SignatureV4({
      service: "lambda",
      region: this.cfg.region,
      credentials: sdkCreds(await this.creds()),
      sha256: Sha256,
    });
    const signed = await signer.sign({
      method: "POST",
      protocol: url.protocol,
      hostname: url.hostname,
      path: url.pathname,
      headers: { host: url.hostname, "content-type": "application/json" },
      body,
    });

    const resp = await fetch(this.cfg.endpoint, {
      method: "POST",
      headers: signed.headers as Record<string, string>,
      body,
    });
    const payload = (await resp.json()) as Record<string, unknown>;
    for (const chunk of responseToChunks(resp.status, payload)) {
      yield chunk;
    }
  }
}
