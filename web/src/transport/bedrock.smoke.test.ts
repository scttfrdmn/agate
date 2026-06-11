// Opt-in live smoke test for the Tier 0 path — mirrors the Python `-m aws` proof.
//
// Skipped unless AGG_LIVE_SMOKE=1. When enabled it needs real scoped credentials
// in the standard AWS env (AWS_ACCESS_KEY_ID / _SECRET_ACCESS_KEY / _SESSION_TOKEN
// — e.g. exported from a broker-vended session) and AGG_SMOKE_MODEL_ID set to a
// model the session is entitled to. It runs a real ConverseStream and asserts we
// receive streamed text. Run:
//
//   AGG_LIVE_SMOKE=1 AGG_SMOKE_MODEL_ID=openai.gpt-oss-20b-1:0 \
//     AWS_REGION=us-east-1 npx vitest run bedrock.smoke
//
// Pure unit tests stay offline; this is the only test that touches Bedrock.

/// <reference types="node" />
import { describe, expect, it } from "vitest";

import type { ScopedCredentials } from "../auth";
import { BedrockTransport } from "./bedrock";

const live = process.env.AGG_LIVE_SMOKE === "1";

describe.skipIf(!live)("BedrockTransport live ConverseStream", () => {
  it("streams text from an entitled model", async () => {
    const region = process.env.AWS_REGION ?? "us-east-1";
    const modelId = process.env.AGG_SMOKE_MODEL_ID;
    expect(modelId, "set AGG_SMOKE_MODEL_ID").toBeTruthy();

    // Use whatever the standard AWS env provides (simulating broker-vended creds).
    const creds = async (): Promise<ScopedCredentials> => ({
      accessKeyId: process.env.AWS_ACCESS_KEY_ID!,
      secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY!,
      sessionToken: process.env.AWS_SESSION_TOKEN ?? "",
      expiration: new Date(Date.now() + 15 * 60_000).toISOString(),
    });

    const transport = new BedrockTransport(region, creds);
    let text = "";
    let reasoning = "";
    let usage: { inputTokens: number; outputTokens: number } | undefined;
    for await (const chunk of transport.converse({
      modelId: modelId!,
      messages: [{ role: "user", content: "Say the single word: ok" }],
      // Generous budget: reasoning models (gpt-oss) spend tokens thinking before
      // they emit answer text, so a tiny cap yields reasoning but no answer.
      maxTokens: 512,
    })) {
      text += chunk.delta;
      if (chunk.reasoning) reasoning += chunk.reasoning;
      if (chunk.usage) usage = chunk.usage;
    }
    // Proof of streaming: we received answer text (reasoning is a bonus channel).
    expect(text.length).toBeGreaterThan(0);
    expect(usage?.outputTokens).toBeGreaterThan(0);
    void reasoning;
  }, 30_000);
});
