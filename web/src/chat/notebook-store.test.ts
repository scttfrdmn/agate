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
      answeredPrompt: "what is enthalpy?",
      meta: { cost: 0.0001, usage: { inputTokens: 5, outputTokens: 3 } },
      state: "idle",
    },
    {
      id: "x2",
      name: "c2",
      kind: "code",
      prompt: "print({{c1}})",
      output: { stdout: "H = U + PV\n", stderr: "", result: "42" },
      state: "running", // transient run state — must not persist
    },
  ],
};

describe("serializeNotebook", () => {
  it("captures durable content and drops transient RUN state", () => {
    const s = serializeNotebook(nb, "Thermo", "2026-07-22T00:00:00Z");
    expect(s.schema).toBe(NOTEBOOK_SCHEMA);
    expect(s.name).toBe("Thermo");
    expect(s.savedAt).toBe("2026-07-22T00:00:00Z");
    expect(s.cells.map((c) => c.kind)).toEqual(["prompt", "code"]);
    expect(s.cells[0].answer).toBe("H = U + PV");
    expect(s.cells[1].output?.result).toBe("42");
    // Transient run state does not leak; the volatile per-cell id is not serialized (diff-stable).
    expect(JSON.stringify(s)).not.toContain("running");
    expect(JSON.stringify(s)).not.toContain('"id"');
    // JSON round-trips cleanly.
    expect(() => JSON.parse(JSON.stringify(s))).not.toThrow();
  });

  it("persists a prompt cell's stale flag + answeredPrompt (freeze/stale under git, #246)", () => {
    const staleNb: Notebook = {
      cells: [
        {
          id: "s1",
          name: "c1",
          kind: "prompt",
          prompt: "edited question",
          answer: "old frozen answer",
          answeredPrompt: "original question",
          stale: true,
          state: "idle",
        },
      ],
    };
    const s = serializeNotebook(staleNb, "N", "t");
    expect(s.cells[0].stale).toBe(true);
    expect(s.cells[0].answeredPrompt).toBe("original question");
  });
});

describe("deserializeNotebook", () => {
  it("round-trips a serialised notebook (fresh ids, idle state)", () => {
    const stored = JSON.parse(JSON.stringify(serializeNotebook(nb, "Thermo", "t")));
    const out = deserializeNotebook(stored)!;
    expect(out.name).toBe("Thermo");
    expect(out.notebook.cells.map((c) => c.kind)).toEqual(["prompt", "code"]);
    expect(out.notebook.cells.map((c) => c.name)).toEqual(["c1", "c2"]);
    expect(out.notebook.cells[1].prompt).toBe("print({{c1}})");
    expect(out.notebook.cells[1].output?.result).toBe("42");
    expect(out.notebook.cells.every((c) => c.state === "idle")).toBe(true);
    // A fresh (non-stale) prompt cell round-trips as not-stale.
    expect(out.notebook.cells[0].stale).toBeUndefined();
    // Fresh ids (not the serialised x1/x2).
    expect(out.notebook.cells[0].id).not.toBe("x1");
  });

  it("reopens a stale cell stale-badged, preserving its answeredPrompt (#246)", () => {
    const stored = {
      schema: 2,
      name: "N",
      savedAt: "t",
      cells: [
        {
          name: "c1",
          kind: "prompt",
          prompt: "edited question",
          answer: "old frozen answer",
          answeredPrompt: "original question",
          stale: true,
        },
      ],
    };
    const out = deserializeNotebook(stored)!;
    const cell = out.notebook.cells[0];
    expect(cell.stale).toBe(true); // still badged — never silently re-run
    expect(cell.answer).toBe("old frozen answer"); // frozen output travels with the cell
    expect(cell.answeredPrompt).toBe("original question"); // stale relationship preserved
  });

  it("reconciles stale from prompt≠answeredPrompt even if the file omits stale (hand-edited/merged)", () => {
    // A git-committable file can be edited/merged into an inconsistent state: prompt no longer
    // matches the prompt that produced the answer, but `stale` is absent. It must reopen stale
    // (a cell + badge), NOT as an authoritative chat turn showing a mismatched Q&A (#246 review).
    const inconsistent = {
      schema: 2,
      name: "N",
      savedAt: "t",
      cells: [{ name: "c1", kind: "prompt", prompt: "edited", answer: "old", answeredPrompt: "original" }],
    };
    const out = deserializeNotebook(inconsistent)!;
    expect(out.notebook.cells[0].stale).toBe(true);
  });

  it("round-trips a stale CODE cell (stale keyed on output, not answer)", () => {
    const staleCode: Notebook = {
      cells: [
        {
          id: "k1",
          name: "c1",
          kind: "code",
          prompt: "x = 2",
          output: { stdout: "", stderr: "", result: "2" },
          stale: true,
          state: "idle",
        },
      ],
    };
    const round = deserializeNotebook(JSON.parse(JSON.stringify(serializeNotebook(staleCode, "N", "t"))))!;
    expect(round.notebook.cells[0].stale).toBe(true); // stale code cell survives (output, not answer)
  });

  it("reads a v1 file (no stale/answeredPrompt) as fresh, matched to its prompt", () => {
    const v1 = {
      schema: 1,
      name: "old",
      savedAt: "t",
      cells: [{ name: "c1", kind: "prompt", prompt: "q", answer: "a" }],
    };
    const out = deserializeNotebook(v1)!;
    expect(out.notebook.cells[0].stale).toBeUndefined();
    expect(out.notebook.cells[0].answeredPrompt).toBe("q"); // v1 fallback: matched to prompt
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
