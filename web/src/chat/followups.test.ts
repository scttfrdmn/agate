import { describe, expect, it } from "vitest";

import { parseFollowups } from "./followups";

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
});
