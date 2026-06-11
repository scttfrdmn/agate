import { describe, expect, it } from "vitest";

import type { ConverseChunk, ConverseRequest, Transport } from "../transport";
import { ChatSession } from "./session";

// A fake transport that streams a scripted set of chunks, recording the request
// it was handed so we can assert the session sends the right history.
class FakeTransport implements Transport {
  readonly tier = "bedrock" as const;
  lastRequest?: ConverseRequest;
  constructor(private readonly chunks: ConverseChunk[]) {}
  async *converse(req: ConverseRequest): AsyncIterable<ConverseChunk> {
    this.lastRequest = req;
    for (const c of this.chunks) yield c;
  }
}

describe("ChatSession", () => {
  it("accumulates streamed deltas, fires onDelta, returns text + usage", async () => {
    const t = new FakeTransport([
      { delta: "Hel", done: false },
      { delta: "lo", done: false },
      { delta: "", done: true, usage: { inputTokens: 5, outputTokens: 2 } },
    ]);
    const s = new ChatSession(t, "openai.gpt-oss-20b-1:0");

    const seen: string[] = [];
    const res = await s.send("hi", { onDelta: (d) => seen.push(d) });

    expect(seen).toEqual(["Hel", "lo"]);
    expect(res.text).toBe("Hello");
    expect(res.usage).toEqual({ inputTokens: 5, outputTokens: 2 });
  });

  it("streams reasoning on a separate channel and never persists it", async () => {
    const t = new FakeTransport([
      { delta: "", reasoning: "let me think", done: false },
      { delta: "answer", done: false },
      { delta: "", done: true },
    ]);
    const s = new ChatSession(t, "openai.gpt-oss-20b-1:0");

    const reasoning: string[] = [];
    const answer: string[] = [];
    const res = await s.send("q", {
      onReasoning: (r) => reasoning.push(r),
      onDelta: (d) => answer.push(d),
    });

    expect(reasoning).toEqual(["let me think"]);
    expect(res.text).toBe("answer");
    // History holds only the answer, not the chain-of-thought.
    expect(s.messages.at(-1)).toEqual({ role: "assistant", content: "answer" });
  });

  it("records the user turn then the assistant turn in history", async () => {
    const t = new FakeTransport([{ delta: "ok", done: true }]);
    const s = new ChatSession(t, "m", "system-prompt");
    await s.send("q1");

    expect(s.messages).toEqual([
      { role: "system", content: "system-prompt" },
      { role: "user", content: "q1" },
      { role: "assistant", content: "ok" },
    ]);
  });

  it("sends prior history (incl. system) to the transport on each turn", async () => {
    const t = new FakeTransport([{ delta: "a1", done: true }]);
    const s = new ChatSession(t, "m", "sys", 256);
    await s.send("q1");
    expect(t.lastRequest?.maxTokens).toBe(256);
    expect(t.lastRequest?.messages.map((m) => m.role)).toEqual(["system", "user"]);
  });

  it("prepends RAG context for the turn without persisting it to history", async () => {
    const t = new FakeTransport([{ delta: "grounded", done: true }]);
    const provider = async (q: string) => [
      { role: "system" as const, content: `context for: ${q}` },
    ];
    const s = new ChatSession(t, "m", undefined, undefined, provider);
    await s.send("what is X?");

    // The transport saw the grounding context first, then the user turn.
    expect(t.lastRequest?.messages.map((m) => m.role)).toEqual(["system", "user"]);
    expect(t.lastRequest?.messages[0].content).toBe("context for: what is X?");
    // History holds only the real turns — context is not persisted.
    expect(s.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
  });
});
