import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../transport";
import { bodyLength, selectContext } from "./context";

const convo: ChatMessage[] = [
  { role: "system", content: "You are helpful." },
  { role: "user", content: "u1" },
  { role: "assistant", content: "a1" },
  { role: "user", content: "u2" },
  { role: "assistant", content: "a2" },
  { role: "user", content: "u3" },
  { role: "assistant", content: "a3" },
];

describe("selectContext", () => {
  it("with no policy, sends everything unchanged", () => {
    expect(selectContext(convo)).toEqual(convo);
  });

  it("floor drops earlier turns but keeps leading system messages", () => {
    // floor=4 → drop u1,a1,u2,a2; keep u3,a3 (+ system head)
    const out = selectContext(convo, { floor: 4 });
    expect(out).toEqual([
      { role: "system", content: "You are helpful." },
      { role: "user", content: "u3" },
      { role: "assistant", content: "a3" },
    ]);
  });

  it("maxTurns keeps only the last N pairs, starting on a user turn", () => {
    const out = selectContext(convo, { maxTurns: 1 });
    expect(out).toEqual([
      { role: "system", content: "You are helpful." },
      { role: "user", content: "u3" },
      { role: "assistant", content: "a3" },
    ]);
  });

  it("maxTurns trims a leading assistant so the window starts on a user", () => {
    // 3 pairs, window of 2 pairs = last 4 msgs = u2,a2,u3,a3 (already starts on user)
    const out = selectContext(convo, { maxTurns: 2 }).filter((m) => m.role !== "system");
    expect(out[0]).toEqual({ role: "user", content: "u2" });
    expect(out).toHaveLength(4);
  });

  it("summary is inserted as a system message ahead of retained turns", () => {
    const out = selectContext(convo, { floor: 4, summary: "we discussed u1 and u2" });
    expect(out[0]).toEqual({ role: "system", content: "You are helpful." });
    expect(out[1].role).toBe("system");
    expect(out[1].content).toContain("we discussed u1 and u2");
    expect(out.slice(2)).toEqual([
      { role: "user", content: "u3" },
      { role: "assistant", content: "a3" },
    ]);
  });

  it("blank summary is ignored", () => {
    const out = selectContext(convo, { floor: 6, summary: "   " });
    expect(out.some((m) => m.content.includes("Summary of earlier"))).toBe(false);
  });

  it("floor + maxTurns compose (floor first, then window the remainder)", () => {
    const out = selectContext(convo, { floor: 2, maxTurns: 1 }).filter((m) => m.role !== "system");
    // floor=2 leaves u2,a2,u3,a3; window of 1 pair → u3,a3
    expect(out).toEqual([
      { role: "user", content: "u3" },
      { role: "assistant", content: "a3" },
    ]);
  });
});

describe("bodyLength", () => {
  it("counts non-system messages", () => {
    expect(bodyLength(convo)).toBe(6);
    expect(bodyLength([{ role: "system", content: "s" }])).toBe(0);
    expect(bodyLength([])).toBe(0);
  });
});
