import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../transport";
import { cellsFromHistory, isEditedSinceRun, newCell } from "./notebook";

describe("cellsFromHistory", () => {
  it("pairs each user message with the following assistant answer", () => {
    const history: ChatMessage[] = [
      { role: "user", content: "what is enthalpy?" },
      { role: "assistant", content: "Enthalpy is H = U + PV." },
      { role: "user", content: "and entropy?" },
      { role: "assistant", content: "Entropy measures disorder." },
    ];
    const cells = cellsFromHistory(history);
    expect(cells.map((c) => c.prompt)).toEqual(["what is enthalpy?", "and entropy?"]);
    expect(cells.map((c) => c.answer)).toEqual([
      "Enthalpy is H = U + PV.",
      "Entropy measures disorder.",
    ]);
    expect(cells.every((c) => c.state === "idle")).toBe(true);
    // Projected turns are always prompt (AI) cells.
    expect(cells.every((c) => c.kind === "prompt")).toBe(true);
    // Each gets a stable {{cN}} reference name in transcript order (#200 slice 3).
    expect(cells.map((c) => c.name)).toEqual(["c1", "c2"]);
    // Without turnMeta, projected cells have no receipt (meta undefined) — the loaded-notebook case.
    expect(cells.every((c) => c.meta === undefined)).toBe(true);
  });

  it("attaches per-turn receipt meta to each answered cell when given (#245)", () => {
    const history: ChatMessage[] = [
      { role: "user", content: "q1" },
      { role: "assistant", content: "a1" },
      { role: "user", content: "q2" },
      { role: "assistant", content: "a2" },
    ];
    const turnMeta = [
      { usage: { inputTokens: 10, outputTokens: 5 }, cost: 0.001, modelId: "m1" },
      { usage: { inputTokens: 20, outputTokens: 8 }, cost: 0.002, modelId: "m2" },
    ];
    const cells = cellsFromHistory(history, turnMeta);
    expect(cells[0].meta?.cost).toBe(0.001);
    expect(cells[0].meta?.usage?.inputTokens).toBe(10);
    expect(cells[1].meta?.modelId).toBe("m2");
  });

  it("stays aligned when a turn had an empty answer (turnMeta is 1:1 with assistant messages)", () => {
    // Regression (#245 review): ChatSession pushes an assistant message even for an empty answer,
    // so turnMeta must have an entry per assistant message. An empty middle turn must not shift
    // later cells' receipts. Here turn 2's answer was empty but still recorded (meta {}).
    const history: ChatMessage[] = [
      { role: "user", content: "q1" },
      { role: "assistant", content: "a1" },
      { role: "user", content: "q2" },
      { role: "assistant", content: "" }, // empty answer (e.g. guardrail-filtered)
      { role: "user", content: "q3" },
      { role: "assistant", content: "a3" },
    ];
    const turnMeta = [{ cost: 0.001 }, {}, { cost: 0.003 }]; // one per assistant message
    const cells = cellsFromHistory(history, turnMeta);
    expect(cells[0].meta?.cost).toBe(0.001);
    expect(cells[1].meta?.cost).toBeUndefined(); // the empty turn's {} — no misattributed cost
    expect(cells[2].meta?.cost).toBe(0.003); // turn 3 keeps its own cost, not shifted
  });

  it("indexes turnMeta by answered-turn order, skipping an unanswered trailing prompt", () => {
    const history: ChatMessage[] = [
      { role: "user", content: "q1" },
      { role: "assistant", content: "a1" },
      { role: "user", content: "q2-unanswered" },
    ];
    const cells = cellsFromHistory(history, [{ cost: 0.005 }]);
    expect(cells[0].meta?.cost).toBe(0.005); // the one answered turn
    expect(cells[1].answer).toBeUndefined(); // trailing unanswered prompt
    expect(cells[1].meta).toBeUndefined();
  });

  it("skips leading system messages (grounding / memory seeds aren't turns)", () => {
    const history: ChatMessage[] = [
      { role: "system", content: "Relevant remembered context: …" },
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ];
    const cells = cellsFromHistory(history);
    expect(cells).toHaveLength(1);
    expect(cells[0].prompt).toBe("hi");
    expect(cells[0].answer).toBe("hello");
  });

  it("keeps a trailing unpaired user message as an answerless cell", () => {
    const cells = cellsFromHistory([{ role: "user", content: "pending?" }]);
    expect(cells).toHaveLength(1);
    expect(cells[0].prompt).toBe("pending?");
    expect(cells[0].answer).toBeUndefined();
  });

  it("returns [] for empty history", () => {
    expect(cellsFromHistory([])).toEqual([]);
  });
});

describe("newCell", () => {
  it("produces an idle, answerless cell with a unique id (prompt by default)", () => {
    const a = newCell("q?");
    const b = newCell();
    expect(a.state).toBe("idle");
    expect(a.kind).toBe("prompt");
    expect(a.prompt).toBe("q?");
    expect(a.answer).toBeUndefined();
    expect(a.id).not.toBe(b.id);
  });

  it("produces a code cell when asked", () => {
    const c = newCell("print(1)", "code");
    expect(c.kind).toBe("code");
    expect(c.prompt).toBe("print(1)");
    expect(c.state).toBe("idle");
  });
});

describe("isEditedSinceRun", () => {
  it("is false for an unanswered cell (nothing to contradict)", () => {
    expect(isEditedSinceRun(newCell("draft"))).toBe(false);
  });
  it("is false when the prompt still matches the answered prompt", () => {
    const c = { ...newCell("q?"), answer: "a", answeredPrompt: "q?" };
    expect(isEditedSinceRun(c)).toBe(false);
  });
  it("is true once the prompt diverges from the answered prompt", () => {
    const c = { ...newCell("edited q?"), answer: "a", answeredPrompt: "q?" };
    expect(isEditedSinceRun(c)).toBe(true);
  });
});
