// Governed-access console view (Phase 9 Track 1, #63 slice 2).
//
// Renders the analytics payload from the admin API (infra/functions/admin) into the
// agate design system. The view is only reached by an admin session; the real gate
// is the 403 the API returns to everyone else. The render is pure DOM (testable in
// happy-dom); the fetch is a thin shim.

export interface AdminUserSpend {
  user: string;
  spend_usd: number;
}
export interface AdminTenant {
  tenant: string;
  period: string;
  total_usd: number;
  user_count: number;
  users: AdminUserSpend[];
}
export interface AdminPayload {
  period: string | null;
  grand_total_usd: number;
  tenant_count: number;
  tenants: AdminTenant[];
  top_users: { id: string; spend_usd: number }[];
}

function el(tag: string, cls = "", text?: string): HTMLElement {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const usd = (n: number): string => `$${(n || 0).toFixed(4)}`;

// Build the analytics dashboard into `target`. Pure: state in -> DOM out.
export function renderAdmin(payload: AdminPayload, target: HTMLElement): void {
  target.replaceChildren();

  const summary = el("section", "panel");
  summary.setAttribute("aria-label", "Usage summary");
  summary.appendChild(el("div", "panel-title", "Total spend"));
  summary.appendChild(el("div", "meter-total", usd(payload.grand_total_usd)));
  summary.appendChild(
    el(
      "div",
      "meter-status",
      `${payload.tenant_count} tenant(s)` + (payload.period ? ` · ${payload.period}` : " · all periods"),
    ),
  );
  target.appendChild(summary);

  // Per-tenant table.
  const tenantsPanel = el("section", "panel");
  tenantsPanel.setAttribute("aria-label", "Spend by tenant");
  tenantsPanel.appendChild(el("div", "panel-title", "Spend by tenant"));
  if (!payload.tenants.length) {
    tenantsPanel.appendChild(el("p", "cost-line", "No usage recorded yet."));
  } else {
    tenantsPanel.appendChild(tenantTable(payload.tenants));
  }
  target.appendChild(tenantsPanel);

  // Top spenders.
  if (payload.top_users.length) {
    const top = el("section", "panel");
    top.setAttribute("aria-label", "Top spenders");
    top.appendChild(el("div", "panel-title", "Top spenders"));
    const ul = el("ul");
    ul.style.cssText = "list-style:none;display:flex;flex-direction:column;gap:.3rem";
    for (const u of payload.top_users) {
      const li = el("li");
      li.style.cssText = "display:flex;justify-content:space-between;gap:1rem";
      li.appendChild(el("span", "", u.id));
      li.appendChild(el("span", "cost-line", usd(u.spend_usd)));
      ul.appendChild(li);
    }
    top.appendChild(ul);
    target.appendChild(top);
  }
}

function tenantTable(tenants: AdminTenant[]): HTMLElement {
  const table = el("table") as HTMLTableElement;
  table.style.cssText = "width:100%;border-collapse:collapse;font-size:.88rem";
  const thead = el("thead");
  const hr = el("tr");
  for (const h of ["Tenant", "Users", "Spend"]) {
    const th = el("th", "", h);
    th.setAttribute("scope", "col");
    th.style.cssText = "text-align:left;padding:.3rem .5rem;color:var(--muted);border-bottom:1px solid var(--border)";
    if (h === "Spend") th.style.textAlign = "right";
    hr.appendChild(th);
  }
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = el("tbody");
  for (const t of tenants) {
    const tr = el("tr");
    const name = el("td", "", t.tenant);
    name.style.cssText = "padding:.35rem .5rem;border-bottom:1px solid var(--border)";
    const users = el("td", "", String(t.user_count));
    users.style.cssText = "padding:.35rem .5rem;border-bottom:1px solid var(--border)";
    const spend = el("td", "", usd(t.total_usd));
    spend.style.cssText =
      "padding:.35rem .5rem;border-bottom:1px solid var(--border);text-align:right;font-variant-numeric:tabular-nums";
    tr.append(name, users, spend);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

// Fetch the analytics from the admin API with the IdP token. Throws on non-200
// (the caller renders the error); a 403 means the session isn't admin.
export async function fetchAdmin(
  adminUrl: string,
  idpToken: string,
  period?: string,
): Promise<AdminPayload> {
  const resp = await fetch(adminUrl, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(period ? { idp_token: idpToken, period } : { idp_token: idpToken }),
  });
  if (!resp.ok) {
    throw new Error(resp.status === 403 ? "not authorized (admin only)" : `admin API ${resp.status}`);
  }
  return (await resp.json()) as AdminPayload;
}
