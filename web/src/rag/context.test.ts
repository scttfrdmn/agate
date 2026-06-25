import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../transport";
import { buildContextMessage, withContext, type RetrievedChunk } from "./context";

const chunk = (text: string, sourceKey?: string): RetrievedChunk => ({
  key: "k",
  text,
  sourceKey,
});

describe("buildContextMessage", () => {
  it("returns null when there are no usable chunks", () => {
    expect(buildContextMessage([])).toBeNull();
    expect(buildContextMessage([chunk("   ")])).toBeNull();
  });

  it("folds chunks into a numbered, cited system message", () => {
    const msg = buildContextMessage([
      chunk("photosynthesis converts light", "chem/bio.txt"),
      chunk("the krebs cycle", "chem/bio2.txt"),
    ])!;
    expect(msg.role).toBe("system");
    expect(msg.content).toContain("[1] (source: chem/bio.txt)");
    expect(msg.content).toContain("[2] (source: chem/bio2.txt)");
    expect(msg.content).toContain("photosynthesis converts light");
    // Grounded-only instruction + a user-legible refusal that names the document scope.
    expect(msg.content).toContain("only this context");
    expect(msg.content).toContain("documents available to this session");
  });

  it("omits the citation when no sourceKey is present", () => {
    const msg = buildContextMessage([chunk("bare context")])!;
    expect(msg.content).toContain("[1]\nbare context");
    expect(msg.content).not.toContain("(source:");
  });
});

describe("withContext", () => {
  const convo: ChatMessage[] = [{ role: "user", content: "what is the krebs cycle?" }];

  it("prepends a context system message when chunks exist", () => {
    const out = withContext(convo, [chunk("the krebs cycle is...", "chem/x.txt")]);
    expect(out).toHaveLength(2);
    expect(out[0].role).toBe("system");
    expect(out[1]).toEqual(convo[0]);
  });

  it("passes the conversation through unchanged when there is no context", () => {
    const out = withContext(convo, []);
    expect(out).toEqual(convo);
  });
});
