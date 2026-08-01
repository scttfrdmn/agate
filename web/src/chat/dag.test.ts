import { describe, expect, it } from "vitest";

import type { NotebookCell } from "./notebook";
import {
  buildDeps,
  dependentsOf,
  nextCellName,
  outputImages,
  referencedNames,
  refsIn,
  resolveSource,
  resolveSourceWithImages,
} from "./dag";

function cell(part: Partial<NotebookCell> & { id: string; name: string }): NotebookCell {
  return { kind: "prompt", prompt: "", state: "idle", ...part };
}

describe("refsIn", () => {
  it("finds known {{name}} references, de-duplicated in order", () => {
    const known = new Set(["c1", "c2"]);
    expect(refsIn("use {{c1}} and {{c2}} and {{c1}} again, ignore {{c9}}", known)).toEqual([
      "c1",
      "c2",
    ]);
  });
  it("tolerates inner whitespace", () => {
    expect(refsIn("{{ c1 }}", new Set(["c1"]))).toEqual(["c1"]);
  });
});

describe("resolveSource", () => {
  it("inlines a prompt cell's reference raw", () => {
    const cells = [
      cell({ id: "1", name: "c1", answer: "Paris is the capital." }),
      cell({ id: "2", name: "c2", prompt: "Summarize: {{c1}}" }),
    ];
    expect(resolveSource(cells[1], cells).resolved).toBe("Summarize: Paris is the capital.");
  });

  it("JSON-encodes a reference inside a code cell (valid Python literal)", () => {
    const cells = [
      cell({ id: "1", name: "c1", answer: 'he said "hi"\nline2' }),
      cell({ id: "2", name: "c2", kind: "code", prompt: "text = {{c1}}" }),
    ];
    const { resolved, deps } = resolveSource(cells[1], cells);
    expect(resolved).toBe('text = "he said \\"hi\\"\\nline2"');
    expect(deps).toEqual(["c1"]);
  });

  it("uses a code cell's result/stdout as its output text", () => {
    const cells = [
      cell({ id: "1", name: "c1", kind: "code", output: { stdout: "ignored\n", stderr: "", result: "42" } }),
      cell({ id: "2", name: "c2", prompt: "value is {{c1}}" }),
    ];
    expect(resolveSource(cells[1], cells).resolved).toBe("value is 42");
  });

  it("leaves unknown and self references untouched", () => {
    const cells = [cell({ id: "2", name: "c2", prompt: "{{c9}} and {{c2}}" })];
    expect(resolveSource(cells[0], cells).resolved).toBe("{{c9}} and {{c2}}");
  });
});

describe("outputImages", () => {
  it("returns a code cell's captured figures, empty for prompt cells", () => {
    const png = "data:image/png;base64,AAAA";
    const code = cell({ id: "1", name: "c1", kind: "code", output: { stdout: "", stderr: "", images: [png] } });
    expect(outputImages(code)).toEqual([png]);
    expect(outputImages(cell({ id: "2", name: "c2", answer: "hi" }))).toEqual([]);
  });
});

describe("resolveSourceWithImages", () => {
  const png = "data:image/png;base64,PLOT";
  it("collects a referenced code cell's figures and leaves a [figure from cN] placeholder", () => {
    const cells = [
      cell({ id: "1", name: "c1", kind: "code", output: { stdout: "", stderr: "", images: [png] } }),
      cell({ id: "2", name: "c2", prompt: "Interpret {{c1}}" }),
    ];
    const { resolved, images, deps } = resolveSourceWithImages(cells[1], cells);
    expect(images).toEqual([png]);
    expect(deps).toEqual(["c1"]);
    expect(resolved).toContain("[figure from c1]");
  });
  it("includes text output alongside the figure placeholder when present", () => {
    const cells = [
      cell({ id: "1", name: "c1", kind: "code", output: { stdout: "T_eq = 500 K\n", stderr: "", images: [png] } }),
      cell({ id: "2", name: "c2", prompt: "Given {{c1}}, explain" }),
    ];
    const { resolved } = resolveSourceWithImages(cells[1], cells);
    expect(resolved).toContain("[figure from c1]");
    expect(resolved).toContain("T_eq = 500 K");
  });
  it("falls back to plain text output when the referenced code cell has no figure", () => {
    const cells = [
      cell({ id: "1", name: "c1", kind: "code", output: { stdout: "", stderr: "", result: "42" } }),
      cell({ id: "2", name: "c2", prompt: "value is {{c1}}" }),
    ];
    const { resolved, images } = resolveSourceWithImages(cells[1], cells);
    expect(resolved).toBe("value is 42");
    expect(images).toEqual([]);
  });

  it("does NOT attach the same figure twice when a cell is referenced twice (#244 M1)", () => {
    const cells = [
      cell({ id: "1", name: "c1", kind: "code", output: { stdout: "", stderr: "", images: [png] } }),
      cell({ id: "2", name: "c2", prompt: "compare {{c1}} early vs {{c1}} late" }),
    ];
    const { images, deps } = resolveSourceWithImages(cells[1], cells);
    expect(images).toEqual([png]); // once, not twice
    expect(deps).toEqual(["c1"]);
  });
});

describe("buildDeps / dependentsOf", () => {
  const chain = () => [
    cell({ id: "1", name: "c1", answer: "a" }),
    cell({ id: "2", name: "c2", prompt: "from {{c1}}", answer: "b" }),
    cell({ id: "3", name: "c3", kind: "code", prompt: "x = {{c2}}" }),
  ];

  it("maps each cell to the ids it references", () => {
    const deps = buildDeps(chain());
    expect(deps.get("2")).toEqual(["1"]);
    expect(deps.get("3")).toEqual(["2"]);
    expect(deps.get("1")).toEqual([]);
  });

  it("returns transitive dependents in topological order", () => {
    const order = dependentsOf(chain(), "1").map((c) => c.id);
    expect(order).toEqual(["2", "3"]); // c2 before c3 (c3 depends on c2)
  });

  it("excludes the changed cell itself and unrelated cells", () => {
    const cells = [
      ...chain(),
      cell({ id: "4", name: "c4", prompt: "independent" }),
    ];
    const ids = dependentsOf(cells, "1").map((c) => c.id);
    expect(ids).not.toContain("1");
    expect(ids).not.toContain("4");
  });

  it("is cycle-safe (mutual references don't loop forever)", () => {
    const cells = [
      cell({ id: "1", name: "c1", prompt: "{{c2}}" }),
      cell({ id: "2", name: "c2", prompt: "{{c1}}" }),
    ];
    const ids = dependentsOf(cells, "1").map((c) => c.id);
    expect(ids).toContain("2"); // terminates, includes the dependent
  });
});

describe("nextCellName", () => {
  it("returns c1 for an empty notebook and max+1 otherwise", () => {
    expect(nextCellName([])).toBe("c1");
    expect(nextCellName([cell({ id: "1", name: "c1" }), cell({ id: "3", name: "c3" })])).toBe("c4");
  });
});

describe("referencedNames", () => {
  it("is empty when no cell references another (names stay hidden)", () => {
    const cells = [cell({ id: "1", name: "c1", prompt: "a" }), cell({ id: "2", name: "c2", prompt: "b" })];
    expect(referencedNames(cells).size).toBe(0);
  });
  it("returns names that another cell references via {{cN}}", () => {
    const cells = [
      cell({ id: "1", name: "c1", prompt: "a" }),
      cell({ id: "2", name: "c2", prompt: "build on {{c1}}" }),
    ];
    expect([...referencedNames(cells)]).toEqual(["c1"]);
  });
  it("ignores self-references and unknown names", () => {
    const cells = [
      cell({ id: "1", name: "c1", prompt: "loop {{c1}} and {{c9}}" }),
      cell({ id: "2", name: "c2", prompt: "b" }),
    ];
    expect(referencedNames(cells).size).toBe(0);
  });
});
