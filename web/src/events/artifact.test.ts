import { describe, expect, it } from "vitest";

import { receiptToCsv, serializeRun } from "./artifact";
import type { RunEvent } from "./protocol";

const CREATED = "2026-06-11T12:00:00Z";
const ROSTER = [
  { tier: "frontier", label: "frontier", max_tokens: 512 },
  { tier: "open-weight-70b", label: "open-weight-70b", max_tokens: 512 },
];

const PANEL_EVENTS: RunEvent[] = [
  { type: "route", mode: "DEBATE" },
  { type: "model", tier: "frontier", label: "frontier", state: "start", pane: "frontier" },
  { type: "model", tier: "ow", label: "open-weight-70b", state: "start", pane: "open-weight-70b" },
  { type: "answer", pane: "frontier", text: "LDL-C drops." },
  { type: "answer", pane: "open-weight-70b", text: "LDL-C falls." },
  { type: "citation", source: "PMC4521", modality: "image", ref: "figure-3", thumb: "QUJD" },
  { type: "citation", source: "DOC1", modality: "text", ref: "p2" },
  {
    type: "divergence",
    summary: "Differ on magnitude.",
    claims: [
      {
        id: "c1", text: "Lowers the marker.", kind: "agreement",
        positions: [{ pane: "frontier", stance: "supports" }], verify: false,
      },
    ],
  },
  { type: "answer", title: "Panel — reconciled", text: "Agree on direction." },
  {
    type: "receipt",
    rows: [
      { label: "panel · frontier", kind: "llm", cost: 0.0012 },
      { label: "panel · open-weight-70b", kind: "llm", cost: 0.0003 },
    ],
    total: 0.0015,
  },
];

describe("serializeRun", () => {
  const art = serializeRun(PANEL_EVENTS, {
    runId: "run-1",
    createdAt: CREATED,
    question: "Does it work?",
    roster: ROSTER,
    costTag: "GRANT-NIH-123",
  });

  it("captures mode, question, roster, and deduped models in first-seen order", () => {
    expect(art.mode).toBe("DEBATE");
    expect(art.question).toBe("Does it work?");
    expect(art.roster).toEqual(ROSTER);
    expect(art.models).toEqual(["frontier", "open-weight-70b"]);
  });

  it("captures the full transcript including the reconciled turn", () => {
    expect(art.transcript).toHaveLength(3);
    expect(art.transcript[2].title).toBe("Panel — reconciled");
  });

  it("captures text and visual citations", () => {
    expect(art.citations).toHaveLength(2);
    expect(art.citations.find((c) => c.modality === "image")?.ref).toBe("figure-3");
  });

  it("captures the divergence structure and receipt total", () => {
    expect(art.divergence?.claims[0].kind).toBe("agreement");
    expect(art.receipt).toHaveLength(2);
    expect(art.cost_total).toBeCloseTo(0.0015);
  });

  it("captures Analyze code cells", () => {
    const a = serializeRun(
      [
        { type: "route", mode: "ANALYSIS" },
        { type: "code", language: "python", source: "print(6*7)" },
      ],
      { runId: "r2", createdAt: CREATED },
    );
    expect(a.code[0].source).toBe("print(6*7)");
  });

  it("ignores unknown event types (forward-compatible)", () => {
    const a = serializeRun(
      [{ type: "answer", text: "hi" }, { type: "novel", x: 1 } as unknown as RunEvent],
      { runId: "r3", createdAt: CREATED },
    );
    expect(a.transcript).toHaveLength(1);
  });
});

describe("receiptToCsv", () => {
  it("tags every row and appends a TOTAL row", () => {
    const art = serializeRun(PANEL_EVENTS, {
      runId: "run-1", createdAt: CREATED, costTag: "GRANT-NIH-123",
    });
    const csv = receiptToCsv(art);
    const lines = csv.trim().split("\n");
    expect(lines[0]).toBe("run_id,cost_tag,label,kind,cost");
    expect(lines.slice(1).every((l) => l.includes("GRANT-NIH-123"))).toBe(true);
    expect(lines[lines.length - 1]).toContain("TOTAL");
    expect(lines[lines.length - 1].endsWith("0.001500")).toBe(true);
  });

  it("escapes a label containing a comma", () => {
    const art = serializeRun(
      [{ type: "receipt", rows: [{ label: "a, b", kind: "llm", cost: 1 }], total: 1 }],
      { runId: "r", createdAt: CREATED },
    );
    expect(receiptToCsv(art)).toContain('"a, b"');
  });
});
