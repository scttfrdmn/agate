// ChatSession — in-memory conversation over a Transport (design §12 Phase 2:
// "no history persistence yet — in-memory"). Cross-device history (DynamoDB
// on-demand) and AgentCore Memory come later; this keeps the turn loop pure and
// transport-agnostic so swapping tiers never touches the chat logic.

import type { ChatMessage, ConverseChunk, Transport } from "../transport";

export interface SendResult {
  text: string;
  usage?: { inputTokens: number; outputTokens: number };
}

export interface SendCallbacks {
  onDelta?: (delta: string) => void;
  onReasoning?: (reasoning: string) => void;
}

export class ChatSession {
  private readonly history: ChatMessage[] = [];

  constructor(
    private readonly transport: Transport,
    private readonly modelId: string,
    system?: string,
    private readonly maxTokens?: number,
  ) {
    if (system) {
      this.history.push({ role: "system", content: system });
    }
  }

  /** A copy of the conversation so far (system + turns). */
  get messages(): ChatMessage[] {
    return [...this.history];
  }

  /**
   * Send a user turn and stream the assistant reply. `onDelta` fires for each
   * answer-text chunk and `onReasoning` for any chain-of-thought (reasoning
   * models only). The accumulated answer is appended to history and returned with
   * final usage; reasoning is shown live but never persisted. The assistant turn
   * is committed only once the stream completes, so a failed stream doesn't poison
   * history with a partial.
   */
  async send(
    userText: string,
    callbacks: SendCallbacks = {},
  ): Promise<SendResult> {
    this.history.push({ role: "user", content: userText });

    let text = "";
    let usage: SendResult["usage"];
    // Snapshot history so the transport never sees a live reference that mutates
    // (we append the assistant turn below) during a lazy stream.
    for await (const chunk of this.transport.converse({
      modelId: this.modelId,
      messages: [...this.history],
      maxTokens: this.maxTokens,
    })) {
      const c: ConverseChunk = chunk;
      if (c.delta) {
        text += c.delta;
        callbacks.onDelta?.(c.delta);
      }
      if (c.reasoning) {
        // Reasoning is shown live but NOT persisted to history — only the answer
        // is part of the conversation the model sees on the next turn.
        callbacks.onReasoning?.(c.reasoning);
      }
      if (c.usage) usage = c.usage;
    }

    this.history.push({ role: "assistant", content: text });
    return { text, usage };
  }
}
