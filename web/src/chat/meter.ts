// Session cost + budget meter for the sidebar. Accumulates this session's spend
// (sum of per-call costs the choke point reports) and renders the period budget the
// server returns, with a visual bar so a user can see where they stand. All figures
// are the server's non-authoritative running estimate (design §7.2) — the real
// authoritative spend is the async log meter; this is the live indication.

import type { BudgetStatus } from "../transport";

export interface MeterEls {
  total: HTMLElement; // big $ this session
  status: HTMLElement; // sub-line under the total
  budgetWrap: HTMLElement; // budget block (hidden until a budget is known)
  budgetBar: HTMLElement; // the fill element
  budgetText: HTMLElement; // "$2.10 of $50.00 · 4%"
}

export class SessionMeter {
  private sessionTotal = 0;

  constructor(private readonly els: MeterEls) {
    this.renderTotal();
    this.els.budgetWrap.hidden = true;
  }

  /** Fold one answered call into the session total + period budget display. */
  record(cost: number | undefined, budget: BudgetStatus | undefined): void {
    if (typeof cost === "number") {
      this.sessionTotal += cost;
      this.renderTotal();
    }
    if (budget) this.renderBudget(budget);
  }

  private renderTotal(): void {
    this.els.total.textContent = `$${this.sessionTotal.toFixed(4)}`;
    this.els.status.textContent = "this session · billed per request";
  }

  private renderBudget(b: BudgetStatus): void {
    if (b.budgetUsd === null || b.budgetUsd <= 0) {
      // No cap configured — show spend for the period, no bar.
      this.els.budgetWrap.hidden = false;
      this.els.budgetBar.style.width = "0%";
      this.els.budgetWrap.dataset.level = "none";
      this.els.budgetText.textContent = `$${b.spendUsd.toFixed(4)} this ${period(b)} · no budget cap set`;
      return;
    }
    const pct = Math.min(100, Math.max(0, (b.spendUsd / b.budgetUsd) * 100));
    this.els.budgetWrap.hidden = false;
    this.els.budgetBar.style.width = `${pct.toFixed(1)}%`;
    this.els.budgetWrap.dataset.level = pct >= 90 ? "high" : pct >= 70 ? "mid" : "ok";
    this.els.budgetText.textContent =
      `$${b.spendUsd.toFixed(4)} of $${b.budgetUsd.toFixed(2)} · ${Math.round(pct)}%`;
  }
}

function period(b: BudgetStatus): string {
  return b.period ? `period (${b.period})` : "period";
}
