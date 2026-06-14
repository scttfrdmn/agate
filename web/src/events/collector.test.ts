import { describe, expect, it } from "vitest";

import { EventCollector, runStateFrom } from "./collector";
import type { RunEvent } from "./protocol";

// A scripted Panel run: two panes read the same evidence, then the adjudicator
// emits a divergence + reconciled answer. Mirrors what run_panel (agate/panel) emits.
const PANEL_RUN: RunEvent[] = [
  { type: "route", mode: "DEBATE" },
  { type: "model", tier: "frontier", label: "frontier", state: "start", pane: "frontier" },
  { type: "model", tier: "open-weight-70b", label: "open-weight-70b", state: "start", pane: "open-weight-70b" },
  { type: "answer", pane: "frontier", text: "LDL-C drops." },
  { type: "answer", pane: "open-weight-70b", text: "LDL-C falls sharply." },
  {
    type: "model", tier: "frontier", label: "frontier", state: "done", pane: "frontier",
    elapsed_s: 2.1, cost: 0.0012, usage: { inputTokens: 800, outputTokens: 120 },
  },
  {
    type: "model", tier: "open-weight-70b", label: "open-weight-70b", state: "done", pane: "open-weight-70b",
    elapsed_s: 1.4, cost: 0.0003, usage: { inputTokens: 800, outputTokens: 110 },
  },
  { type: "cost", total: 0.0015 },
  {
    type: "divergence",
    summary: "Agree on direction, differ on magnitude.",
    claims: [
      {
        id: "c1", text: "Treatment lowers LDL-C.", kind: "agreement",
        positions: [
          { pane: "frontier", stance: "supports" },
          { pane: "open-weight-70b", stance: "supports" },
        ],
        verify: false, evidence_refs: ["DOC1"],
      },
      {
        id: "c2", text: "The reduction is clinically large.", kind: "disagreement",
        positions: [
          { pane: "frontier", stance: "partial", note: "trial-dependent" },
          { pane: "open-weight-70b", stance: "supports" },
        ],
        verify: true,
      },
    ],
  },
  { type: "answer", title: "Panel — reconciled", text: "Agree on direction, differ on magnitude." },
];

describe("EventCollector", () => {
  it("collects events in order and filters by type", () => {
    const c = new EventCollector();
    PANEL_RUN.forEach(c.emit);
    expect(c.events).toHaveLength(PANEL_RUN.length);
    expect(c.ofType("model")).toHaveLength(4);
    expect(c.ofType("divergence")).toHaveLength(1);
  });
});

describe("runStateFrom (Panel)", () => {
  const state = runStateFrom(PANEL_RUN);

  it("maps each model to its own pane in first-seen order", () => {
    expect(state.mode).toBe("DEBATE");
    expect(state.panes.map((p) => p.label)).toEqual(["frontier", "open-weight-70b"]);
  });

  it("accumulates per-pane answer text and final done/cost", () => {
    const frontier = state.panes.find((p) => p.label === "frontier")!;
    expect(frontier.text).toBe("LDL-C drops.");
    expect(frontier.state).toBe("done");
    expect(frontier.cost).toBeCloseTo(0.0012);
    expect(frontier.usage?.outputTokens).toBe(120);
  });

  it("captures the divergence payload with per-pane positions", () => {
    expect(state.divergence?.claims).toHaveLength(2);
    const disagreement = state.divergence!.claims.find((c) => c.kind === "disagreement")!;
    expect(disagreement.verify).toBe(true);
    expect(disagreement.positions.map((p) => p.pane)).toEqual(["frontier", "open-weight-70b"]);
  });

  it("keeps the reconciled answer separate from per-pane text", () => {
    // The titled reconciled answer has no pane, so it lands in state.answer.
    expect(state.answer).toBe("Agree on direction, differ on magnitude.");
  });

  it("tracks the running cost total", () => {
    expect(state.costTotal).toBeCloseTo(0.0015);
  });
});

describe("runStateFrom (Ask, single stream)", () => {
  it("accumulates a single answer with no panes", () => {
    const events: RunEvent[] = [
      { type: "route", mode: "SYNTHESIS" },
      { type: "answer", text: "Photosynthesis " },
      { type: "answer", text: "converts light." },
    ];
    const state = runStateFrom(events);
    expect(state.mode).toBe("SYNTHESIS");
    expect(state.panes).toHaveLength(0);
    expect(state.answer).toBe("Photosynthesis converts light.");
  });
});

describe("runStateFrom (Analyze)", () => {
  it("builds an editable cell and attaches the chart to the latest cell", () => {
    const events: RunEvent[] = [
      { type: "route", mode: "ANALYSIS" },
      { type: "code", language: "python", source: "import numpy as np\nprint(np.mean([1,2,3]))" },
      { type: "chart", mime: "image/png", data: "QUJD" },
    ];
    const state = runStateFrom(events);
    expect(state.cells).toHaveLength(1);
    expect(state.cells[0].source).toContain("np.mean");
    expect(state.cells[0].chart).toEqual({ mime: "image/png", data: "QUJD" });
  });
});

describe("backward compatibility", () => {
  it("ignores unknown event types without throwing (forward-compatible)", () => {
    const events: RunEvent[] = [
      { type: "answer", text: "hi" },
      { type: "something_new", foo: 1 } as unknown as RunEvent,
    ];
    const state = runStateFrom(events);
    expect(state.answer).toBe("hi");
  });

  it("treats a model event without a pane as a single-column run", () => {
    const events: RunEvent[] = [
      { type: "model", tier: "frontier", label: "solo", state: "start" },
      { type: "answer", pane: "solo", text: "ok" },
      { type: "model", tier: "frontier", label: "solo", state: "done" },
    ];
    const state = runStateFrom(events);
    expect(state.panes).toHaveLength(1);
    expect(state.panes[0].text).toBe("ok");
  });
});
