// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { type AdminPayload, renderAdmin } from "./view";

const PAYLOAD: AdminPayload = {
  period: "2026-06",
  grand_total_usd: 6,
  tenant_count: 2,
  tenants: [
    {
      tenant: "kempner",
      period: "2026-06",
      total_usd: 4,
      user_count: 1,
      users: [{ user: "carol", spend_usd: 4 }],
    },
    {
      tenant: "chem",
      period: "2026-06",
      total_usd: 2,
      user_count: 2,
      users: [
        { user: "alice", spend_usd: 1.5 },
        { user: "bob", spend_usd: 0.5 },
      ],
    },
  ],
  top_users: [
    { id: "kempner/carol", spend_usd: 4 },
    { id: "chem/alice", spend_usd: 1.5 },
  ],
};

describe("renderAdmin", () => {
  it("renders total, tenant table, and top spenders", () => {
    const target = document.createElement("div");
    renderAdmin(PAYLOAD, target);
    expect(target.querySelector(".meter-total")?.textContent).toBe("$6.0000");
    const rows = target.querySelectorAll("tbody tr");
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("kempner");
    expect(target.textContent).toContain("kempner/carol");
  });

  it("uses scoped table semantics (col headers)", () => {
    const target = document.createElement("div");
    renderAdmin(PAYLOAD, target);
    const headers = target.querySelectorAll('th[scope="col"]');
    expect(headers).toHaveLength(3);
  });

  it("handles an empty tenant list gracefully", () => {
    const target = document.createElement("div");
    renderAdmin({ ...PAYLOAD, tenants: [], tenant_count: 0, top_users: [] }, target);
    expect(target.textContent).toContain("No usage recorded yet.");
  });
});
