import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../transport";
import { cellsFromHistory, newCell } from "./notebook";

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
