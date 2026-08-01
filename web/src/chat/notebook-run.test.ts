import { describe, expect, it } from "vitest";

import type { Emit, RunEvent } from "../events/protocol";
import type { AgentInvocation } from "../transport/agentcore";
import { AgentCoreTransport, parseEventBlob } from "../transport/agentcore";
import type { ChatMessage, ConverseChunk, ConverseRequest, Transport } from "../transport";
import { runAgentCell, runCell } from "./notebook-run";

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

// A fake AgentCore transport that records the invocation and replays a canned RunEvent blob.
class FakeAgentCore extends AgentCoreTransport {
  lastInvocation?: AgentInvocation;
  constructor(private readonly blob: string) {
    super({ region: "us-east-1", runtimeArn: "arn:aws:bedrock-agentcore:::runtime/x" }, async () => ({
      accessKeyId: "AK",
      secretAccessKey: "SK",
      sessionToken: "TOK",
      expiration: new Date(Date.now() + 900_000).toISOString(),
    }));
  }
  async run(inv: AgentInvocation, emit: Emit): Promise<void> {
    this.lastInvocation = inv;
    for (const e of parseEventBlob(this.blob)) emit(e as RunEvent);
  }
}

describe("runAgentCell (#248)", () => {
  it("threads the cap into the invocation (snake_case) and parses the agent-cell receipt", async () => {
    const blob =
      '{"type":"answer","text":"here is what I found"}\n' +
      '{"type":"receipt","kind":"agent-cell","answer_cap_bounded":true,' +
      '"stop_reason":"budget cap reached at cell cap","spent_usd":1.87,' +
      '"elapsed_seconds":142.5,"steps_taken":6}\n';
    const t = new FakeAgentCore(blob);
    const seen: string[] = [];
    const res = await runAgentCell(t, "tok-123", "scan the literature", { costUsd: 2, seconds: 300, maxSteps: 8 }, (d) => seen.push(d));

    // Cap is serialised to the backend's snake_case shape; the token is forwarded.
    expect(t.lastInvocation?.cap).toEqual({ cost_usd: 2, seconds: 300, max_steps: 8 });
    expect(t.lastInvocation?.idp_token).toBe("tok-123");
    expect(t.lastInvocation?.question).toBe("scan the literature");
    // Answer accumulates; receipt is parsed onto all three axes + cap-bounded.
    expect(res.text).toBe("here is what I found");
    expect(seen).toEqual(["here is what I found"]);
    expect(res.receipt).toEqual({
      capBounded: true,
      stopReason: "budget cap reached at cell cap",
      spentUsd: 1.87,
      elapsedSeconds: 142.5,
      stepsTaken: 6,
    });
  });

  it("maps an unset cap axis to null (uncapped on that axis)", async () => {
    const t = new FakeAgentCore('{"type":"answer","text":"ok"}\n');
    await runAgentCell(t, "tok", "q", { costUsd: 1 });
    expect(t.lastInvocation?.cap).toEqual({ cost_usd: 1, seconds: null, max_steps: null });
  });

  it("falls back to a zero-spend, non-bounded receipt when none is emitted", async () => {
    const t = new FakeAgentCore('{"type":"answer","text":"answer, no receipt"}\n');
    const res = await runAgentCell(t, "tok", "q", { costUsd: 1 });
    expect(res.text).toBe("answer, no receipt");
    expect(res.receipt).toEqual({
      capBounded: false,
      stopReason: "completed",
      spentUsd: 0,
      elapsedSeconds: 0,
      stepsTaken: 0,
    });
  });
});
