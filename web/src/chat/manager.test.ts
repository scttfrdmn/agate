// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { ChatManager } from "./manager";
import type { ConverseRequest, ConverseChunk, Transport } from "../transport";

// A fake transport that echoes a fixed answer, so we can drive ChatSession.send.
const fakeTransport: Transport = {
  tier: "openai",
  async *converse(_req: ConverseRequest): AsyncIterable<ConverseChunk> {
    yield { delta: "an answer", done: false };
    yield { delta: "", done: true, usage: { inputTokens: 5, outputTokens: 3 } };
  },
};

function hosts() {
  const appendHost = document.createElement("div");
  const scrollHost = document.createElement("div");
  const listHost = document.createElement("div");
  document.body.append(appendHost, scrollHost, listHost);
  return { appendHost, scrollHost, listHost, transport: fakeTransport };
}

describe("ChatManager", () => {
  it("starts with one chat and renders it in the list", () => {
    const m = new ChatManager(hosts());
    expect(m.current.title).toBe("New chat");
    expect(m.current.turns).toBe(0);
  });

  it("newChat creates and switches to a fresh chat; only it is visible", () => {
    const d = hosts();
    const m = new ChatManager(d);
    const first = m.current.id;
    const second = m.newChat();
    expect(second.id).not.toBe(first);
    expect(m.current.id).toBe(second.id);
    // exactly one visible pane (the others are hidden)
    const panes = Array.from(d.appendHost.querySelectorAll(".chat-pane"));
    expect(panes.length).toBe(2);
    expect(panes.filter((p) => !(p as HTMLElement).hidden).length).toBe(1);
  });

  it("switchTo shows the chosen chat and marks its list item active", () => {
    const d = hosts();
    const m = new ChatManager(d);
    const a = m.current.id;
    m.newChat();
    m.switchTo(a);
    expect(m.current.id).toBe(a);
    const active = d.listHost.querySelector(".chat-list-item.active");
    expect(active).not.toBeNull();
  });

  it("recordTurn titles the chat from the first question and counts turns", () => {
    const m = new ChatManager(hosts());
    m.recordTurn("What is the first law of thermodynamics about energy?", "…");
    expect(m.current.turns).toBe(1);
    expect(m.current.title.startsWith("What is the first law")).toBe(true);
    m.recordTurn("and the second?", "…");
    expect(m.current.turns).toBe(2);
    // title stays from the first question
    expect(m.current.title.startsWith("What is the first law")).toBe(true);
  });

  it("sessionFor rebuilds the session on a model change but keeps history", async () => {
    const m = new ChatManager(hosts());
    const s1 = m.sessionFor("openai.gpt-oss-20b-1:0");
    await s1.send("hi"); // pushes user+assistant into the shared history array
    const s2 = m.sessionFor("us.anthropic.claude-haiku-4-5-20251001-v1:0");
    expect(s2).not.toBe(s1); // rebuilt for the new model
    // the rebuilt session carries the prior turns
    expect(s2.messages.length).toBeGreaterThanOrEqual(2);
  });
});
