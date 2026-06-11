import { describe, expect, it } from "vitest";

import { toConverseMessages } from "./bedrock";
import type { ChatMessage } from "./index";

describe("toConverseMessages", () => {
  it("splits a system message into the Converse system field", () => {
    const msgs: ChatMessage[] = [
      { role: "system", content: "be terse" },
      { role: "user", content: "hi" },
    ];
    const { system, messages } = toConverseMessages(msgs);
    expect(system).toEqual([{ text: "be terse" }]);
    expect(messages).toHaveLength(1);
    expect(messages[0].role).toBe("user");
    expect(messages[0].content).toEqual([{ text: "hi" }]);
  });

  it("preserves user/assistant turn order and wraps text as content blocks", () => {
    const msgs: ChatMessage[] = [
      { role: "user", content: "q1" },
      { role: "assistant", content: "a1" },
      { role: "user", content: "q2" },
    ];
    const { system, messages } = toConverseMessages(msgs);
    expect(system).toEqual([]);
    expect(messages.map((m) => m.role)).toEqual(["user", "assistant", "user"]);
    expect(messages[1].content).toEqual([{ text: "a1" }]);
  });

  it("handles an empty conversation", () => {
    const { system, messages } = toConverseMessages([]);
    expect(system).toEqual([]);
    expect(messages).toEqual([]);
  });
});
