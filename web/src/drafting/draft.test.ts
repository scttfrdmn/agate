// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { type DraftPlan, renderDraft, responseToPlan } from "./draft";

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
  };

  it("renders the bounded plan lines + a confirm button", () => {
    const target = document.createElement("div");
    renderDraft(okPlan, target);
    expect(target.textContent).toContain("chemistry/chem-101");
    expect(target.textContent).toContain("≤ $20");
    const btn = target.querySelector("button");
    expect(btn?.textContent).toContain("Confirm");
  });

  it("invokes onConfirm and disables the button on confirm", () => {
    const target = document.createElement("div");
    let confirmed = false;
    renderDraft(okPlan, target, { onConfirm: () => (confirmed = true) });
    const btn = target.querySelector("button") as HTMLButtonElement;
    btn.click();
    expect(confirmed).toBe(true);
    expect(btn.disabled).toBe(true);
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
