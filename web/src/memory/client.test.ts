import { describe, expect, it } from "vitest";

import { recallToContext, recordText } from "./client";

describe("recordText", () => {
  it("reads plain content / text / memoryContent keys", () => {
    expect(recordText({ content: "a fact" })).toBe("a fact");
    expect(recordText({ text: "  spaced  " })).toBe("spaced");
    expect(recordText({ memoryContent: "summary" })).toBe("summary");
  });

  it("reads a nested {text} object (AgentCore record shape)", () => {
    expect(recordText({ content: { text: "nested fact" } })).toBe("nested fact");
  });

  it("returns '' when there's nothing usable", () => {
    expect(recordText({})).toBe("");
    expect(recordText({ content: 42 })).toBe("");
  });
});

describe("recallToContext", () => {
  it("renders records into a remembered-context block", () => {
    const block = recallToContext([
      { content: "the user studies thermodynamics" },
      { text: "prefers concise answers" },
    ]);
    expect(block).toBe(
      "Relevant remembered context:\n- the user studies thermodynamics\n- prefers concise answers",
    );
  });

  it("drops empty records and returns '' when nothing remains", () => {
    expect(recallToContext([{}, { content: "" }])).toBe("");
    expect(recallToContext([])).toBe("");
  });
});
