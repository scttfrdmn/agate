// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import {
  type DeployResult,
  type DraftPlan,
  renderDraft,
  responseToDeploy,
  responseToPlan,
} from "./draft";

describe("responseToPlan", () => {
  it("maps a 200 ok plan", () => {
    const p = responseToPlan(200, {
      ok: true,
      reason: "",
      plan: ["reads chemistry/chem-101", "≤ $20 / user / month"],
    });
    expect(p.ok).toBe(true);
    expect(p.plan).toHaveLength(2);
  });

  it("carries the validated spec on ok (echoed to confirm), ignoring a non-object", () => {
    const ok = responseToPlan(200, { ok: true, plan: [], spec: { agent: "x" } });
    expect(ok.spec).toEqual({ agent: "x" });
    const bad = responseToPlan(200, { ok: true, plan: [], spec: "nope" });
    expect(bad.spec).toBeUndefined();
  });

  it("maps a 200 rejected draft (the clamp working) to ok=false + reason", () => {
    const p = responseToPlan(200, {
      ok: false,
      reason: "draft requests authority outside your own — clamped/rejected",
      plan: [],
    });
    expect(p.ok).toBe(false);
    expect(p.reason).toContain("outside your own");
  });

  it("maps a 403 not-entitled to a readable rejection (not a throw)", () => {
    const p = responseToPlan(403, { error: "not_entitled", detail: "token verification failed" });
    expect(p.ok).toBe(false);
    expect(p.reason).toBe("token verification failed");
  });

  it("maps a 500 to a rejection", () => {
    const p = responseToPlan(500, { error: "drafting_error" });
    expect(p.ok).toBe(false);
    expect(p.reason).toBe("drafting_error");
  });

  it("defends against a malformed plan field", () => {
    const p = responseToPlan(200, { ok: true, plan: "not an array" });
    expect(p.plan).toEqual([]);
  });
});

describe("renderDraft", () => {
  const okPlan: DraftPlan = {
    ok: true,
    reason: "",
    plan: ["reads chemistry/chem-101", "may draft summaries", "≤ $20 / user / month"],
    spec: { agent: "paper-sweep", scope: "chemistry/chem-101" },
  };

  it("renders the bounded plan lines + a confirm button", () => {
    const target = document.createElement("div");
    renderDraft(okPlan, target);
    expect(target.textContent).toContain("chemistry/chem-101");
    expect(target.textContent).toContain("≤ $20");
    const btn = target.querySelector("button");
    expect(btn?.textContent).toContain("Confirm");
  });

  it("calls onConfirm with the spec and renders the created agent id", async () => {
    const target = document.createElement("div");
    let gotSpec: Record<string, unknown> | null = null;
    const onConfirm = async (spec: Record<string, unknown>): Promise<DeployResult> => {
      gotSpec = spec;
      return { ok: true, reason: "", agentId: "uni/paper-sweep", plan: [] };
    };
    renderDraft(okPlan, target, { onConfirm });
    const btn = target.querySelector("button") as HTMLButtonElement;
    btn.click();
    await Promise.resolve(); // let the async onClick settle
    await Promise.resolve();
    expect(gotSpec).toEqual(okPlan.spec);
    expect(target.textContent).toContain("Created: uni/paper-sweep");
  });

  it("surfaces a deploy rejection and re-enables the button", async () => {
    const target = document.createElement("div");
    const onConfirm = async (): Promise<DeployResult> => ({
      ok: false,
      reason: "draft could not be bounded",
      agentId: "",
      plan: [],
    });
    renderDraft(okPlan, target, { onConfirm });
    const btn = target.querySelector("button") as HTMLButtonElement;
    btn.click();
    await Promise.resolve();
    await Promise.resolve();
    expect(target.textContent).toContain("Not created");
    expect(btn.disabled).toBe(false); // can retry
  });

  it("explains when deploy isn't wired (no onConfirm)", () => {
    const target = document.createElement("div");
    renderDraft(okPlan, target);
    const btn = target.querySelector("button") as HTMLButtonElement;
    btn.click();
    expect(target.textContent).toContain("not enabled");
    expect(btn.disabled).toBe(true);
  });

  it("does not offer a working confirm when the plan has no spec", () => {
    const target = document.createElement("div");
    const onConfirm = async (): Promise<DeployResult> => ({
      ok: true,
      reason: "",
      agentId: "x",
      plan: [],
    });
    renderDraft({ ...okPlan, spec: undefined }, target, { onConfirm });
    const btn = target.querySelector("button") as HTMLButtonElement;
    btn.click();
    expect(target.textContent).toContain("not enabled"); // canDeploy false without a spec
  });

  it("renders a rejection plainly (the clamp is the point, not an error)", () => {
    const target = document.createElement("div");
    renderDraft(
      { ok: false, reason: "draft requests authority outside your own", plan: [] },
      target,
    );
    expect(target.textContent).toContain("clamped to your authority");
    expect(target.textContent).toContain("outside your own");
    // no confirm button on a rejected draft
    expect(target.querySelector("button")).toBeNull();
  });
});

describe("responseToDeploy", () => {
  it("maps a 200 created result", () => {
    const r = responseToDeploy(200, {
      ok: true,
      agent_id: "uni/paper-sweep",
      plan: ["reads chemistry/chem-101"],
    });
    expect(r.ok).toBe(true);
    expect(r.agentId).toBe("uni/paper-sweep");
    expect(r.plan).toEqual(["reads chemistry/chem-101"]);
  });

  it("maps a 200 re-clamp rejection to ok=false", () => {
    const r = responseToDeploy(200, { ok: false, reason: "draft could not be bounded" });
    expect(r.ok).toBe(false);
    expect(r.reason).toContain("bounded");
    expect(r.agentId).toBe("");
  });

  it("maps a 403 to a readable rejection (not a throw)", () => {
    const r = responseToDeploy(403, { error: "not_entitled", detail: "token verification failed" });
    expect(r.ok).toBe(false);
    expect(r.reason).toBe("token verification failed");
  });

  it("maps a 500 to a rejection", () => {
    const r = responseToDeploy(500, { error: "deploy_error" });
    expect(r.ok).toBe(false);
    expect(r.reason).toBe("deploy_error");
  });
});
