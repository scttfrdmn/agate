import { describe, expect, it } from "vitest";

import { CostMeter } from "./cost";

describe("CostMeter (SPA live estimate)", () => {
  it("computes LLM dollars from tokens with config rates", () => {
    const m = new CostMeter({ modelRates: { frontier: { inputPerMtok: 3, outputPerMtok: 15 } } });
    const cost = m.addLlm("panel · frontier", "frontier", {
      inputTokens: 1_000_000,
      outputTokens: 1_000_000,
    });
    expect(cost).toBeCloseTo(18.0);
    expect(m.total).toBeCloseTo(18.0);
  });

  it("falls back to default rates and never crashes on unknown ids", () => {
    const m = new CostMeter();
    expect(m.addLlm("x", "frontier", { inputTokens: 0, outputTokens: 0 })).toBe(0);
    // unknown id resolves to a default rather than throwing
    expect(m.addLlm("y", "no-such-model", { inputTokens: 1_000_000, outputTokens: 0 })).toBeGreaterThan(0);
  });

  it("meters compute and retrieval distinctly from tokens", () => {
    const m = new CostMeter({ computePerSec: 0.0002, retrievalPerK: 0.25 });
    expect(m.addCompute("analyze", 10)).toBeCloseTo(0.002);
    expect(m.addRetrieval("rag", 1000)).toBeCloseTo(0.25);
    const r = m.receipt();
    expect(r.rows.map((x) => x.kind).sort()).toEqual(["compute", "retrieval"]);
  });

  it("matches the Python engine: 1000 in @0.10 + 500 out @0.40 = 0.0003", () => {
    const m = new CostMeter({ modelRates: { oss: { inputPerMtok: 0.1, outputPerMtok: 0.4 } } });
    expect(m.addLlm("ask", "oss", { inputTokens: 1000, outputTokens: 500 })).toBeCloseTo(0.0003);
  });
});
