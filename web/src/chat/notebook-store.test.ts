import { describe, expect, it } from "vitest";

import type { Notebook } from "./notebook";
import { NOTEBOOK_SCHEMA, deserializeNotebook, serializeNotebook } from "./notebook-store";

const nb: Notebook = {
  cells: [
    {
      id: "x1",
      name: "c1",
      kind: "prompt",
      prompt: "what is enthalpy?",
      answer: "H = U + PV",
      meta: { cost: 0.0001, usage: { inputTokens: 5, outputTokens: 3 } },
      state: "idle",
    },
    {
      id: "x2",
      name: "c2",
      kind: "code",
      prompt: "print({{c1}})",
      output: { stdout: "H = U + PV\n", stderr: "", result: "42" },
      state: "running", // transient — must not persist
      stale: true, // transient — must not persist
    },
  ],
};

describe("serializeNotebook", () => {
  it("captures durable content and drops transient state", () => {
    const s = serializeNotebook(nb, "Thermo", "2026-07-22T00:00:00Z");
    expect(s.schema).toBe(NOTEBOOK_SCHEMA);
    expect(s.name).toBe("Thermo");
    expect(s.savedAt).toBe("2026-07-22T00:00:00Z");
    expect(s.cells.map((c) => c.kind)).toEqual(["prompt", "code"]);
    expect(s.cells[0].answer).toBe("H = U + PV");
    expect(s.cells[1].output?.result).toBe("42");
    // No transient fields leak into the stored shape.
    expect(JSON.stringify(s)).not.toContain("running");
    expect(JSON.stringify(s)).not.toContain("stale");
    // JSON round-trips cleanly.
    expect(() => JSON.parse(JSON.stringify(s))).not.toThrow();
  });
});

describe("deserializeNotebook", () => {
  it("round-trips a serialised notebook (fresh ids, idle state, no stale)", () => {
    const stored = JSON.parse(JSON.stringify(serializeNotebook(nb, "Thermo", "t")));
    const out = deserializeNotebook(stored)!;
    expect(out.name).toBe("Thermo");
    expect(out.notebook.cells.map((c) => c.kind)).toEqual(["prompt", "code"]);
    expect(out.notebook.cells.map((c) => c.name)).toEqual(["c1", "c2"]);
    expect(out.notebook.cells[1].prompt).toBe("print({{c1}})");
    expect(out.notebook.cells[1].output?.result).toBe("42");
    expect(out.notebook.cells.every((c) => c.state === "idle")).toBe(true);
    expect(out.notebook.cells.every((c) => c.stale === undefined)).toBe(true);
    // Fresh ids (not the serialised x1/x2).
    expect(out.notebook.cells[0].id).not.toBe("x1");
  });

  it("defaults an unknown kind to prompt and tolerates missing fields", () => {
    const out = deserializeNotebook({ cells: [{ prompt: "hi" }, { kind: "weird", prompt: "yo" }] })!;
    expect(out.notebook.cells.map((c) => c.kind)).toEqual(["prompt", "prompt"]);
    expect(out.name).toBe("Untitled notebook");
  });

  it("returns null for non-notebook payloads and newer schemas", () => {
    expect(deserializeNotebook(null)).toBeNull();
    expect(deserializeNotebook({ nope: true })).toBeNull();
    expect(deserializeNotebook({ schema: 999, cells: [] })).toBeNull();
  });
});
