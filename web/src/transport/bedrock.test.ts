import { describe, expect, it } from "vitest";

import { pngDataUriToBytes, toConverseMessages } from "./bedrock";
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

  it("attaches a PNG figure as an image content block after the text (#244)", () => {
    // 1x1 transparent PNG.
    const png =
      "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";
    const msgs: ChatMessage[] = [{ role: "user", content: "interpret this plot", images: [png] }];
    const { messages } = toConverseMessages(msgs);
    const content = messages[0].content ?? [];
    expect(content).toHaveLength(2);
    expect(content[0]).toEqual({ text: "interpret this plot" });
    const img = content[1] as { image?: { format: string; source: { bytes: Uint8Array } } };
    expect(img.image?.format).toBe("png");
    expect(img.image?.source.bytes).toBeInstanceOf(Uint8Array);
    expect(img.image?.source.bytes.length).toBeGreaterThan(0);
  });

  it("skips a non-PNG / malformed image data-URI (never forwards an arbitrary blob)", () => {
    const msgs: ChatMessage[] = [
      { role: "user", content: "x", images: ["javascript:alert(1)", "data:text/html;base64,xxxx"] },
    ];
    const { messages } = toConverseMessages(msgs);
    expect(messages[0].content ?? []).toEqual([{ text: "x" }]); // only text, no image block
  });
});

describe("pngDataUriToBytes", () => {
  it("decodes a real base64 PNG data-URI to bytes", () => {
    const realPng =
      "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";
    const bytes = pngDataUriToBytes(realPng);
    expect(bytes).toBeInstanceOf(Uint8Array);
    expect(bytes!.length).toBeGreaterThan(8); // header + payload
  });
  it("returns null for a non-PNG or non-data-URI value", () => {
    expect(pngDataUriToBytes("data:image/jpeg;base64,AAAA")).toBeNull();
    expect(pngDataUriToBytes("not a uri")).toBeNull();
    expect(pngDataUriToBytes("")).toBeNull();
  });
  it("rejects the png prefix wrapping non-PNG bytes (magic-byte check, #244 M2)", () => {
    const notPng = "data:image/png;base64," + btoa("not a real png");
    expect(pngDataUriToBytes(notPng)).toBeNull();
    // A real PNG (1x1) passes the magic check.
    const realPng =
      "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";
    expect(pngDataUriToBytes(realPng)).toBeInstanceOf(Uint8Array);
  });
});
