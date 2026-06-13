// CostMeter — SPA port (design §7.2). The browser runs the SAME engine as the
// server for a LIVE, NON-AUTHORITATIVE cost estimate shown as a running receipt.
// It carries NO authority: the enforced number is computed server-side from
// invocation logs (cost/ in Python); this is display only. Mirrors cost/meter.py.

export interface ModelRate {
  inputPerMtok: number;
  outputPerMtok?: number;
}

// Hard-default rates (USD per million tokens / per-1k queries / per-second),
// keyed by the logical tier the roster uses. Neutral labels; override via config.
const DEFAULT_MODEL_RATES: Record<string, ModelRate> = {
  oss: { inputPerMtok: 0.1, outputPerMtok: 0.4 },
  mid: { inputPerMtok: 0.8, outputPerMtok: 4.0 },
  frontier: { inputPerMtok: 3.0, outputPerMtok: 15.0 },
  router: { inputPerMtok: 0.1, outputPerMtok: 0.4 },
  "embed-text": { inputPerMtok: 0.02 },
  "embed-multimodal": { inputPerMtok: 0.06 },
};
const DEFAULT_RETRIEVAL_PER_K = 0.25;
const DEFAULT_COMPUTE_PER_SEC = 0.0002;

export interface PriceBook {
  modelRates?: Record<string, ModelRate>;
  retrievalPerK?: number;
  computePerSec?: number;
}

export type CostKind = "llm" | "embedding" | "retrieval" | "compute";

export interface CostRow {
  label: string;
  kind: CostKind;
  cost: number;
}

function round6(x: number): number {
  return Math.round(x * 1e6) / 1e6;
}

function llmRate(pb: PriceBook, modelId: string): ModelRate {
  return (
    pb.modelRates?.[modelId] ??
    DEFAULT_MODEL_RATES[modelId] ??
    DEFAULT_MODEL_RATES.oss // unknown id -> cheapest default, never crash
  );
}

export class CostMeter {
  private readonly rowsList: CostRow[] = [];
  private totalCost = 0;

  constructor(private readonly pb: PriceBook = {}) {}

  get total(): number {
    return this.totalCost;
  }

  get rows(): CostRow[] {
    return [...this.rowsList];
  }

  private add(row: CostRow): number {
    this.rowsList.push(row);
    this.totalCost = round6(this.totalCost + row.cost);
    return row.cost;
  }

  addLlm(label: string, tier: string, usage: { inputTokens: number; outputTokens: number }): number {
    const rate = llmRate(this.pb, tier);
    const cost = round6(
      (usage.inputTokens / 1e6) * rate.inputPerMtok +
        (usage.outputTokens / 1e6) * (rate.outputPerMtok ?? 0),
    );
    return this.add({ label, kind: "llm", cost });
  }

  addCompute(label: string, seconds: number): number {
    const rate = this.pb.computePerSec ?? DEFAULT_COMPUTE_PER_SEC;
    return this.add({ label, kind: "compute", cost: round6(seconds * rate) });
  }

  addRetrieval(label: string, queries = 1): number {
    const rate = this.pb.retrievalPerK ?? DEFAULT_RETRIEVAL_PER_K;
    return this.add({ label, kind: "retrieval", cost: round6((queries / 1000) * rate) });
  }

  receipt(): { rows: CostRow[]; total: number } {
    return { rows: this.rows, total: round6(this.totalCost) };
  }
}
