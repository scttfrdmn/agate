import { describe, expect, it } from "vitest";

import type { ConverseRequest } from "./index";
import { buildRequestBody, responseToChunks } from "./openai";

const scope = {
  tenant: "chem",
  user: "student-7",
  period: "2026-06",
  tier: "oss",
  courses: ["CHEM-101"],
  budget: 100,
};

describe("buildRequestBody", () => {
  it("maps a ConverseRequest + scope into the Tier 1 request body", () => {
    const req: ConverseRequest = {
      modelId: "oss",
      messages: [
        { role: "system", content: "be terse" },
        { role: "user", content: "hi" },
      ],
      maxTokens: 256,
    };
    const body = buildRequestBody(req, scope, "tok-abc");
    expect(body.idp_token).toBe("tok-abc");
    expect(body.model).toBe("oss");
    expect(body.max_tokens).toBe(256);
    expect(body.tenant).toBe("chem");
    expect(body.user).toBe("student-7");
    expect(body.budget).toBe(100);
    expect((body.messages as Array<{ role: string }>).map((m) => m.role)).toEqual([
      "system",
      "user",
    ]);
  });

  it("defaults max_tokens when unset", () => {
    const body = buildRequestBody({ modelId: "oss", messages: [] }, scope, "tok-abc");
    expect(body.max_tokens).toBe(1024);
  });
});

describe("responseToChunks", () => {
  it("maps a 200 allow response to text + usage chunks", () => {
    const chunks = responseToChunks(200, {
      text: "answer",
      usage: { inputTokens: 12, outputTokens: 8 },
    });
    expect(chunks[0]).toEqual({ delta: "answer", done: false });
    expect(chunks[1].done).toBe(true);
    expect(chunks[1].usage).toEqual({ inputTokens: 12, outputTokens: 8 });
  });

  it("surfaces a 402 budget rejection as terminal answer text", () => {
    const chunks = responseToChunks(402, { error: "budget_rejected", detail: "would exceed budget" });
    expect(chunks).toHaveLength(1);
    expect(chunks[0].done).toBe(true);
    expect(chunks[0].delta).toContain("[budget]");
    expect(chunks[0].delta).toContain("would exceed budget");
  });

  it("surfaces other errors without throwing", () => {
    const chunks = responseToChunks(500, { error: "chokepoint_error" });
    expect(chunks[0].delta).toContain("[error]");
    expect(chunks[0].done).toBe(true);
  });
});
