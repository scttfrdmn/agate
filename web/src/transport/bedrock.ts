// Tier 0 adapter — browser-direct Bedrock ConverseStream, SigV4-signed with the
// short-lived scoped credentials from the broker (design §2). The credentials are
// the user's own, narrowed by agg: session tags; this adapter holds NO long-lived
// secret and contains NO authorization logic — IAM enforces the model scope.

import {
  BedrockRuntimeClient,
  ConverseStreamCommand,
  type ContentBlock,
  type Message,
} from "@aws-sdk/client-bedrock-runtime";

import type { ScopedCredentials } from "../auth";
import type { ChatMessage, ConverseChunk, ConverseRequest, Transport } from "./index";

// Pure: map our transport-level messages to the Bedrock Converse wire shape.
// A `system` message becomes the Converse `system` field (returned separately);
// user/assistant turns become `messages`. No SDK calls — unit-testable.
export function toConverseMessages(messages: ChatMessage[]): {
  system: { text: string }[];
  messages: Message[];
} {
  const system: { text: string }[] = [];
  const out: Message[] = [];
  for (const m of messages) {
    if (m.role === "system") {
      system.push({ text: m.content });
      continue;
    }
    const content: ContentBlock[] = [{ text: m.content }];
    out.push({ role: m.role, content });
  }
  return { system, messages: out };
}

// Adapt boto-style ScopedCredentials to the SDK credential provider shape.
function toSdkCredentials(c: ScopedCredentials) {
  return {
    accessKeyId: c.accessKeyId,
    secretAccessKey: c.secretAccessKey,
    sessionToken: c.sessionToken,
    expiration: new Date(c.expiration),
  };
}

export class BedrockTransport implements Transport {
  readonly tier = "bedrock" as const;

  constructor(
    private readonly region: string,
    private readonly creds: () => Promise<ScopedCredentials>,
  ) {}

  private async client(): Promise<BedrockRuntimeClient> {
    // A fresh provider per call so each request signs with current (refreshed)
    // creds; the SDK caches the resolved value within a request.
    return new BedrockRuntimeClient({
      region: this.region,
      credentials: async () => toSdkCredentials(await this.creds()),
    });
  }

  async *converse(req: ConverseRequest): AsyncIterable<ConverseChunk> {
    const client = await this.client();
    const { system, messages } = toConverseMessages(req.messages);

    const command = new ConverseStreamCommand({
      modelId: req.modelId,
      messages,
      system: system.length ? system : undefined,
      inferenceConfig: req.maxTokens ? { maxTokens: req.maxTokens } : undefined,
    });

    const response = await client.send(command);
    if (!response.stream) {
      // Model returned no stream — emit a terminal empty chunk rather than hang.
      yield { delta: "", done: true };
      return;
    }

    for await (const event of response.stream) {
      const delta = event.contentBlockDelta?.delta;
      if (delta?.text) {
        yield { delta: delta.text, done: false };
      }
      // Reasoning models (gpt-oss, DeepSeek-R1-distill, …) stream chain-of-thought
      // here before the answer text. Surface it separately, never as answer text.
      if (delta?.reasoningContent?.text) {
        yield { delta: "", reasoning: delta.reasoningContent.text, done: false };
      }
      if (event.metadata?.usage) {
        // Final usage — feeds the non-authoritative client-side cost estimate
        // (design §7.2). Authority is recomputed server-side from invocation logs.
        const u = event.metadata.usage;
        yield {
          delta: "",
          done: true,
          usage: {
            inputTokens: u.inputTokens ?? 0,
            outputTokens: u.outputTokens ?? 0,
          },
        };
      }
    }
  }
}
