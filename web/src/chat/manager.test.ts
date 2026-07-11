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

function hosts(confirmDelete: (title: string) => boolean = () => true) {
  const appendHost = document.createElement("div");
  const scrollHost = document.createElement("div");
  const listHost = document.createElement("div");
  document.body.append(appendHost, scrollHost, listHost);
  return { appendHost, scrollHost, listHost, transport: fakeTransport, confirmDelete };
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
    const active = d.listHost.querySelector(".chat-list-row.active");
    expect(active).not.toBeNull();
  });

  it("deleteChat removes a chat and switches to a neighbour when the active one goes", () => {
    const d = hosts();
    const m = new ChatManager(d);
    const a = m.current.id;
    const b = m.newChat().id; // active = b
    m.deleteChat(b);
    expect(m.current.id).toBe(a);
    expect(d.appendHost.querySelectorAll(".chat-pane").length).toBe(1);
  });

  it("deleting the last chat starts a fresh empty one", () => {
    const m = new ChatManager(hosts());
    const only = m.current.id;
    m.deleteChat(only);
    expect(m.current.id).not.toBe(only);
    expect(m.current.turns).toBe(0);
  });

  it("delete of a chat with turns is gated by confirmDelete", () => {
    const d = hosts(() => false); // user declines
    const m = new ChatManager(d);
    m.recordTurn("a real question about thermodynamics?", "…");
    const id = m.current.id;
    m.newChat();
    // rebuild list, find the delete button for the first (content) chat
    const del = d.listHost.querySelectorAll<HTMLButtonElement>(".chat-list-delete")[0];
    del.click();
    // declined → still present
    expect(d.appendHost.querySelectorAll(".chat-pane").length).toBe(2);
    void id;
  });

  it("renameChat updates the title (ignoring blank)", () => {
    const m = new ChatManager(hosts());
    const id = m.current.id;
    m.renameChat(id, "  Thermo notes  ");
    expect(m.current.title).toBe("Thermo notes");
    m.renameChat(id, "   ");
    expect(m.current.title).toBe("Thermo notes"); // blank ignored
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

  it("notebookFor lazily projects the chat history into cells (built once)", async () => {
    const m = new ChatManager(hosts());
    await m.sessionFor("openai.gpt-oss-20b-1:0").send("q?");
    const nb1 = m.notebookFor();
    expect(nb1.cells.length).toBe(1);
    expect(nb1.cells[0].prompt).toBe("q?");
    // built once — same object on the next call (per-cell edits survive a toggle)
    expect(m.notebookFor()).toBe(nb1);
  });

  it("setView toggles the active chat's chat vs notebook pane", () => {
    const d = hosts();
    const m = new ChatManager(d);
    const id = m.current.id;
    // default: chat pane shown, notebook hidden
    expect(m.current.el.hidden).toBe(false);
    expect(m.current.notebookEl.hidden).toBe(true);
    m.setView(id, "notebook");
    expect(m.current.el.hidden).toBe(true);
    expect(m.current.notebookEl.hidden).toBe(false);
    m.setView(id, "chat");
    expect(m.current.el.hidden).toBe(false);
    expect(m.current.notebookEl.hidden).toBe(true);
  });

  it("notebook state is per-chat (a fresh chat has its own notebook)", async () => {
    const m = new ChatManager(hosts());
    await m.sessionFor("openai.gpt-oss-20b-1:0").send("first?");
    const nbA = m.notebookFor();
    m.newChat();
    const nbB = m.notebookFor();
    expect(nbB).not.toBe(nbA);
    expect(nbB.cells.length).toBe(0); // fresh chat, empty history
  });
});
