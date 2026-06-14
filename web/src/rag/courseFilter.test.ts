import { describe, expect, it } from "vitest";

import { courseFilter } from "./retriever";

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
