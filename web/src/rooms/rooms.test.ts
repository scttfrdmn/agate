// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { type RoomView, toPostResult, toRoomView } from "./client";
import { renderMembers, renderMessage, renderMessages } from "./view";

describe("toRoomView", () => {
  it("maps a 200 room view", () => {
    const v = toRoomView(200, {
      ok: true,
      room: "lab1",
      scope: "chemistry/chem-101",
      tier: "oss",
      members: [{ kind: "human", subject: "prof" }],
      messages: [{ author: "prof", kind: "human", text: "hi" }],
      cursor: 1,
    });
    expect(v.ok).toBe(true);
    expect(v.scope).toBe("chemistry/chem-101");
    expect(v.members).toHaveLength(1);
    expect(v.cursor).toBe(1);
  });

  it("maps a 403 (disjoint/non-member) to ok=false + reason, not a throw", () => {
    const v = toRoomView(403, { error: "not_entitled", detail: "not a member of this room" });
    expect(v.ok).toBe(false);
    expect(v.reason).toBe("not a member of this room");
  });

  it("defends against malformed members/messages", () => {
    const v = toRoomView(200, { ok: true, members: "nope", messages: 7 });
    expect(v.members).toEqual([]);
    expect(v.messages).toEqual([]);
  });
});

describe("toPostResult", () => {
  it("maps a 200 accepted post", () => {
    const r = toPostResult(200, { ok: true, cursor: 3, message: { author: "prof", kind: "human", text: "x" } });
    expect(r.ok).toBe(true);
    expect(r.cursor).toBe(3);
    expect(r.message?.author).toBe("prof");
  });

  it("maps a 200 budget rejection to ok=false + reason", () => {
    const r = toPostResult(200, { ok: false, reason: "over budget at 'prof': ..." });
    expect(r.ok).toBe(false);
    expect(r.reason).toContain("over budget");
  });

  it("maps a 403 to a readable rejection", () => {
    const r = toPostResult(403, { error: "not_entitled", detail: "not a member of this room" });
    expect(r.ok).toBe(false);
    expect(r.reason).toBe("not a member of this room");
  });
});

describe("renderMembers / renderMessages", () => {
  const view: RoomView = {
    ok: true,
    room: "lab1",
    scope: "chemistry/chem-101",
    tier: "oss",
    members: [
      { kind: "human", subject: "prof" },
      { kind: "agent", subject: "uni/paper-sweep" },
    ],
    messages: [
      { author: "prof", kind: "human", text: "kick off" },
      { author: "uni/paper-sweep", kind: "agent", text: "summary…" },
    ],
    cursor: 2,
  };

  it("renders the participant list + the scope/tier ceiling", () => {
    const t = document.createElement("div");
    renderMembers(view, t);
    expect(t.textContent).toContain("chemistry/chem-101");
    expect(t.textContent).toContain("prof · human");
    expect(t.textContent).toContain("uni/paper-sweep · agent");
  });

  it("renders attributed messages (author + kind + text) via textContent (no HTML sink)", () => {
    const t = document.createElement("div");
    renderMessages(view.messages, t);
    expect(t.querySelectorAll(".agate-pane")).toHaveLength(2);
    expect(t.textContent).toContain("kick off");
    expect(t.textContent).toContain("summary…");
    // a script-y author/text is rendered inert (textContent), never parsed as HTML
    const evil = renderMessage({ author: "<img src=x onerror=alert(1)>", kind: "human", text: "<b>x</b>" });
    expect(evil.querySelector("img")).toBeNull();
    expect(evil.querySelector("b")).toBeNull();
    expect(evil.textContent).toContain("<b>x</b>");
  });

  it("shows an empty-state when there are no messages", () => {
    const t = document.createElement("div");
    renderMessages([], t);
    expect(t.textContent).toContain("No messages yet");
  });
});
