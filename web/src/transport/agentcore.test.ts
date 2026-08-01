import { describe, expect, it } from "vitest";

import type { RunEvent } from "../events/protocol";
import { AgentCoreTransport, parseEventBlob } from "./agentcore";

describe("parseEventBlob", () => {
  it("parses newline-delimited JSON events", () => {
    const blob =
      '{"type":"route","mode":"DEBATE"}\n{"type":"answer","text":"hi"}\n';
    const events = parseEventBlob(blob);
    expect(events).toHaveLength(2);
    expect(events[0]).toEqual({ type: "route", mode: "DEBATE" });
    expect(events[1]).toEqual({ type: "answer", text: "hi" });
  });

  it("skips blank and malformed lines", () => {
    const blob = '{"type":"answer","text":"a"}\n\nnot json\n{"type":"cost","total":1}\n';
    const events = parseEventBlob(blob);
    expect(events.map((e) => e.type)).toEqual(["answer", "cost"]);
  });

  it("handles an empty blob", () => {
    expect(parseEventBlob("")).toEqual([]);
  });

  it("parses the agent-cell receipt event (#248) discriminated by kind", () => {
    const blob =
      '{"type":"answer","title":"partial (cap-bounded)","text":"best effort"}\n' +
      '{"type":"receipt","kind":"agent-cell","answer_cap_bounded":true,"stop_reason":"time cap reached (300s)","spent_usd":0.42,"elapsed_seconds":301,"steps_taken":4}\n';
    const events = parseEventBlob(blob);
    expect(events.map((e) => e.type)).toEqual(["answer", "receipt"]);
    const receipt = events[1];
    expect(receipt.type === "receipt" && "kind" in receipt && receipt.kind).toBe("agent-cell");
  });
});

// A fake AgentCore client: AgentCoreTransport doesn't expose injection, so we
// exercise the decode path via run() against a stubbed send by subclassing.
class FakeAgentCoreTransport extends AgentCoreTransport {
  constructor(private readonly blob: string) {
    super({ region: "us-east-1", runtimeArn: "arn:aws:bedrock-agentcore:::runtime/x" }, async () => ({
      accessKeyId: "AK",
      secretAccessKey: "SK",
      sessionToken: "TOK",
      expiration: new Date(Date.now() + 900_000).toISOString(),
    }));
  }
  // Override run() to bypass the network and decode a canned blob.
  async run(_inv: unknown, emit: (e: RunEvent) => void): Promise<void> {
    for (const e of parseEventBlob(this.blob)) emit(e);
  }
}

describe("AgentCoreTransport.run (decode path)", () => {
  it("emits the decoded RunEvent stream", async () => {
    const blob =
      '{"type":"route","mode":"DEBATE"}\n' +
      '{"type":"model","tier":"frontier","label":"frontier","state":"done","pane":"frontier"}\n' +
      '{"type":"divergence","summary":"s","claims":[]}\n';
    const t = new FakeAgentCoreTransport(blob);
    const events: RunEvent[] = [];
    await t.run({ question: "compare" }, (e) => events.push(e));
    expect(events.map((e) => e.type)).toEqual(["route", "model", "divergence"]);
  });
});
