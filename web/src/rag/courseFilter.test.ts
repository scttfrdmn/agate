import { describe, expect, it } from "vitest";

import { courseFilter, scopeFilter } from "./retriever";

// Mirrors agate.rag.course_filter (Python) — the browser and any server-side
// retriever must scope identically.
describe("courseFilter", () => {
  it("with no courses, only tenant-wide docs are visible (fail-closed)", () => {
    expect(courseFilter([])).toEqual({ course: { $exists: false } });
    expect(courseFilter(undefined)).toEqual({ course: { $exists: false } });
  });

  it("with enrolled courses, includes tenant-wide OR enrolled", () => {
    const f = courseFilter(["chem-101", "chem-102"]) as { $or: unknown[] };
    expect(f.$or).toContainEqual({ course: { $exists: false } });
    expect(f.$or).toContainEqual({ course: { $in: ["chem-101", "chem-102"] } });
  });

  it("drops empty course ids", () => {
    const f = courseFilter(["chem-101", ""]) as { $or: { course: { $in: string[] } }[] };
    const inBranch = f.$or.find((b) => "course" in b && "$in" in b.course)!;
    expect(inBranch.course.$in).toEqual(["chem-101"]);
  });
});

// Hierarchical scope (#70) — mirrors agate.rag.scope_filter.
describe("scopeFilter", () => {
  it("no nodes -> only true tenant-wide docs (no scope AND no course)", () => {
    expect(scopeFilter([])).toEqual({
      $and: [{ scope_ancestors: { $exists: false } }, { course: { $exists: false } }],
    });
  });

  it("nodes -> subtree membership ($in over scope_ancestors) + flat-course back-compat", () => {
    const f = scopeFilter(["chemistry", "chemistry/chem-101"]) as { $or: unknown[] };
    expect(f.$or).toContainEqual({ scope_ancestors: { $in: ["chemistry", "chemistry/chem-101"] } });
    expect(f.$or).toContainEqual({ course: { $in: ["chemistry", "chemistry/chem-101"] } });
  });
});
