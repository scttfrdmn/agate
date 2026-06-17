// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { buildSpecFromForm } from "./builder";

describe("buildSpecFromForm", () => {
  it("assembles a spec, omitting empty optional fields", () => {
    const spec = buildSpecFromForm({
      agent: " paper-sweep ",
      description: " summarize ",
      scope: "chemistry/chem-101",
      reasoning: "lit-review",
      tools: ["library-search"],
      budget: "$20 / user / month",
    });
    expect(spec).toEqual({
      agent: "paper-sweep",
      description: "summarize",
      role: "member",
      scope: "chemistry/chem-101",
      reasoning: "lit-review",
      tools: ["library-search"],
      budget: "$20 / user / month",
    });
  });

  it("omits empty optionals so parse_spec defaults apply", () => {
    const spec = buildSpecFromForm({
      agent: "minimal",
      description: "d",
      scope: "",
      tools: [],
    });
    expect(spec).toEqual({ agent: "minimal", description: "d", role: "member" });
    expect("scope" in spec).toBe(false);
    expect("tools" in spec).toBe(false);
    expect("budget" in spec).toBe(false);
    expect("reasoning" in spec).toBe(false);
  });

  it("trims and copies the tools array (no shared mutation)", () => {
    const tools = ["a", "b"];
    const spec = buildSpecFromForm({ agent: "x", description: "d", scope: "", tools });
    (spec.tools as string[]).push("c");
    expect(tools).toEqual(["a", "b"]); // original untouched
  });

  it("does not let the form set a role above member (tier is server-clamped)", () => {
    // role is fixed to member; the server derives tier from the verified token + mins it.
    const spec = buildSpecFromForm({ agent: "x", description: "d", scope: "", tools: [] });
    expect(spec.role).toBe("member");
  });
});
