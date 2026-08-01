// Tier 0 adapter — browser-direct Bedrock ConverseStream, SigV4-signed with the
// short-lived scoped credentials from the broker (design §2). The credentials are
// the user's own, narrowed by agate: session tags; this adapter holds NO long-lived
// secret and contains NO authorization logic — IAM enforces the model scope.

import {
  BedrockRuntimeClient,
  ConverseStreamCommand,
  type ContentBlock,
  type Message,
} from "@aws-sdk/client-bedrock-runtime";

import type { ScopedCredentials } from "../auth";
import { toSdkCredentials } from "../auth/sdkCreds";
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
    // Attach any inline PNG figures (#244) as Converse image blocks so a vision model can see
    // them. A malformed / non-PNG data-URI is skipped (never sent as an arbitrary blob).
    for (const dataUri of m.images ?? []) {
      const bytes = pngDataUriToBytes(dataUri);
      if (bytes) content.push({ image: { format: "png", source: { bytes } } });
    }
    out.push({ role: m.role, content });
  }
  return { system, messages: out };
}

// PNG magic bytes + a decoded-size ceiling (~3.75 MB, Bedrock's per-image limit). A saved notebook
// is an untrusted source of these strings (open reloads cell.output), so validate the DECODED bytes
// (magic + size), not just the data-URI prefix.
const PNG_MAGIC = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const MAX_IMAGE_BYTES = 3_750_000;

// Decode a `data:image/png;base64,...` URI to bytes for a Converse image block. Returns null for
// anything that isn't a real base64 PNG within the size limit (so we never forward an unexpected
// blob). Pure.
export function pngDataUriToBytes(dataUri: string): Uint8Array | null {
  const prefix = "data:image/png;base64,";
  if (typeof dataUri !== "string" || !dataUri.startsWith(prefix)) return null;
  try {
    const bin = atob(dataUri.slice(prefix.length));
    if (bin.length > MAX_IMAGE_BYTES) return null;
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    if (bytes.length < PNG_MAGIC.length || PNG_MAGIC.some((b, i) => bytes[i] !== b)) return null;
    return bytes;
  } catch {
    return null;
  }
}


export class BedrockTransport implements Transport {
  readonly tier = "bedrock" as const;

  constructor(
    private readonly region: string,
    private readonly creds: () => Promise<ScopedCredentials>,
    // Optional per-call attribution attached as Bedrock requestMetadata, so the
    // invocation log can be metered per tenant/user (#77). An attribution hint,
    // not a security boundary — IAM still enforces the real model/tenant scope.
    private readonly metadata?: () => Record<string, string> | undefined,
  ) {}

  private requestMetadata(): Record<string, string> | undefined {
    const m = this.metadata?.();
    if (!m) return undefined;
    // Bedrock requestMetadata values: [a-zA-Z0-9\s:_@$#=/+,.-], <=256.
    const clean: Record<string, string> = {};
    for (const [k, v] of Object.entries(m)) {
      if (v) clean[k] = String(v).replace(/[^a-zA-Z0-9\s:_@$#=/+,.-]/g, "-").slice(0, 256);
    }
    return Object.keys(clean).length ? clean : undefined;
  }

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
      requestMetadata: this.requestMetadata(),
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
