import { describe, expect, it } from "vitest";

import type { ChatMessage, ConverseChunk, ConverseRequest, Transport } from "../transport";
import { runCell } from "./notebook-run";

class FakeTransport implements Transport {
  readonly tier = "openai" as const;
  lastRequest?: ConverseRequest;
  constructor(private readonly chunks: ConverseChunk[]) {}
  async *converse(req: ConverseRequest): AsyncIterable<ConverseChunk> {
    this.lastRequest = req;
    for (const c of this.chunks) yield c;
  }
}

describe("runCell", () => {
  it("sends exactly ONE user message (no history) and accumulates the result", async () => {
    const t = new FakeTransport([
      { delta: "Par", done: false },
      { delta: "is", done: false },
      {
        delta: "",
        done: true,
        usage: { inputTokens: 7, outputTokens: 2 },
        cost: 0.0003,
        model: "openai.gpt-oss-20b-1:0",
      },
    ]);
    const seen: string[] = [];
    const res = await runCell(t, "auto", "capital of France?", undefined, (d) => seen.push(d));

    expect(t.lastRequest?.messages).toEqual([{ role: "user", content: "capital of France?" }]);
    expect(seen).toEqual(["Par", "is"]);
    expect(res.text).toBe("Paris");
    expect(res.usage).toEqual({ inputTokens: 7, outputTokens: 2 });
    expect(res.cost).toBe(0.0003);
    expect(res.model).toBe("openai.gpt-oss-20b-1:0");
  });

  it("prepends grounding messages ahead of the prompt (RAG / memory)", async () => {
    const t = new FakeTransport([{ delta: "ok", done: true }]);
    const grounding: ChatMessage[] = [{ role: "system", content: "CONTEXT: …" }];
    await runCell(t, "auto", "q?", async () => grounding);
    expect(t.lastRequest?.messages).toEqual([
      { role: "system", content: "CONTEXT: …" },
      { role: "user", content: "q?" },
    ]);
  });

  it("passes the 'auto' model id through unchanged (server routes)", async () => {
    const t = new FakeTransport([{ delta: "x", done: true }]);
    await runCell(t, "auto", "q?");
    expect(t.lastRequest?.modelId).toBe("auto");
  });
});
