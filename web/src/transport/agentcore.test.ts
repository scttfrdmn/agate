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
