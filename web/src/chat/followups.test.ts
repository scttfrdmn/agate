import { describe, expect, it } from "vitest";

import type { ConverseChunk, ConverseRequest, Transport } from "../transport";
import { parseFollowups, stripMathDelimiters, suggestFollowups } from "./followups";

class CaptureTransport implements Transport {
  readonly tier = "openai" as const;
  lastRequest?: ConverseRequest;
  constructor(private readonly reply: string) {}
  async *converse(req: ConverseRequest): AsyncIterable<ConverseChunk> {
    this.lastRequest = req;
    yield { delta: this.reply, done: true };
  }
}

describe("parseFollowups", () => {
  it("parses a plain newline list of questions", () => {
    const out = parseFollowups(
      "What is enthalpy?\nHow does entropy relate to it?\nWhat makes a reaction spontaneous?",
    );
    expect(out).toEqual([
      "What is enthalpy?",
      "How does entropy relate to it?",
      "What makes a reaction spontaneous?",
    ]);
  });

  it("strips numbering, bullets, and surrounding quotes", () => {
    const out = parseFollowups('1. "What is enthalpy?"\n- How does it differ from heat?\n2) Why?');
    expect(out).toEqual(["What is enthalpy?", "How does it differ from heat?", "Why?"]);
  });

  it("ignores non-question lines and preamble", () => {
    const out = parseFollowups("Here are some follow-ups:\nWhat is Gibbs free energy?\nThanks!");
    expect(out).toEqual(["What is Gibbs free energy?"]);
  });

  it("caps at max and de-duplicates case-insensitively", () => {
    const out = parseFollowups("Why?\nWhy?\nHow?\nWhen?\nWhere?", 3);
    expect(out).toEqual(["Why?", "How?", "When?"]);
  });

  it("returns [] when there are no question-shaped lines", () => {
    expect(parseFollowups("no questions here\njust statements.")).toEqual([]);
  });

  it("strips LaTeX math delimiters so chips are readable (and submit-safe)", () => {
    const out = parseFollowups(
      "What is the value of \\(W\\) when heated?\n" +
        "How is \\[dU = Q - W\\] derived?\n" +
        "Why is $G = H - TS$ negative?",
    );
    expect(out).toEqual([
      "What is the value of W when heated?",
      "How is dU = Q - W derived?",
      "Why is G = H - TS negative?",
    ]);
    // No raw delimiters survive (these land verbatim in the prompt on click).
    expect(out.join(" ")).not.toMatch(/\\\(|\\\)|\\\[|\\\]|\$/);
  });
});

describe("stripMathDelimiters", () => {
  it("unwraps each delimiter style to its inner expression", () => {
    expect(stripMathDelimiters("a \\(x\\) b")).toBe("a x b");
    expect(stripMathDelimiters("a \\[y\\] b")).toBe("a y b");
    expect(stripMathDelimiters("a $z$ b")).toBe("a z b");
    expect(stripMathDelimiters("a $$w$$ b")).toBe("a w b");
  });

  it("leaves plain text (and bare currency) untouched", () => {
    expect(stripMathDelimiters("no math here")).toBe("no math here");
    // A lone $ with a space after is not treated as an inline-math open.
    expect(stripMathDelimiters("costs $ 5 total")).toBe("costs $ 5 total");
  });
});

describe("suggestFollowups grounding", () => {
  it("without context, uses the open-ended prompt (no CONTEXT block)", async () => {
    const t = new CaptureTransport("Q1?\nQ2?\nQ3?");
    const r = await suggestFollowups(t, "m", "q?", "a");
    const sent = t.lastRequest!.messages[0].content;
    expect(sent).not.toContain("CONTEXT:");
    expect(sent).toContain("most likely to ask next");
    expect(r.questions).toEqual(["Q1?", "Q2?", "Q3?"]);
  });

  it("with corpus context, includes it and constrains to answerable questions", async () => {
    const t = new CaptureTransport("What is enthalpy defined as?\nHow is dG computed?");
    await suggestFollowups(t, "m", "q?", "a", "Enthalpy is H = U + PV. Gibbs G = H - TS.");
    const sent = t.lastRequest!.messages[0].content;
    expect(sent).toContain("CONTEXT:");
    expect(sent).toContain("H = U + PV");
    expect(sent).toContain("answerable FROM THE CONTEXT");
  });

  it("treats blank/whitespace context as no context", async () => {
    const t = new CaptureTransport("Q?");
    await suggestFollowups(t, "m", "q?", "a", "   ");
    expect(t.lastRequest!.messages[0].content).not.toContain("CONTEXT:");
  });
});
