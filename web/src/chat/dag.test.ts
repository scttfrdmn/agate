import { describe, expect, it } from "vitest";

import type { NotebookCell } from "./notebook";
import { buildDeps, dependentsOf, nextCellName, refsIn, resolveSource } from "./dag";

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
