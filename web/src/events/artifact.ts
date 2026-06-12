// "Save run" — serialise a run event stream into a reproducible, citable record
// (§10.2.8). Mirrors agg/artifact.py's RunArtifact shape so a browser-saved
// artifact and a server-saved one are interchangeable. Pure — no DOM, no network;
// unit-testable. The browser POSTs the JSON to be stored, then the run shows an
// `artifact` event with the returned URL.

import type {
  DivergencePayload,
  ReceiptRow,
  RunEvent,
  RouteMode,
} from "./protocol";

export interface TranscriptTurn {
  pane?: string;
  title?: string;
  text: string;
}

export interface ArtifactCitation {
  source: string;
  modality: string;
  ref: string;
  thumb?: string;
}

export interface CodeCell {
  language: string;
  source: string;
}

export interface RunArtifact {
  run_id: string;
  created_at: string; // ISO-8601, supplied by the caller
  mode?: RouteMode;
  question?: string;
  roster: Array<Record<string, unknown>>;
  models: string[];
  transcript: TranscriptTurn[];
  code: CodeCell[];
  citations: ArtifactCitation[];
  divergence?: DivergencePayload;
  receipt: ReceiptRow[];
  cost_total: number;
  cost_tag?: string;
}

export interface SerializeOptions {
  runId: string;
  createdAt: string;
  question?: string;
  roster?: Array<Record<string, unknown>>;
  costTag?: string;
}

// Fold an ordered event stream into a RunArtifact. Reads the same event shapes the
// reducer renders (§10.2.9). Pure: no clock, no I/O — created_at/run_id are inputs.
export function serializeRun(events: RunEvent[], opts: SerializeOptions): RunArtifact {
  const artifact: RunArtifact = {
    run_id: opts.runId,
    created_at: opts.createdAt,
    question: opts.question,
    roster: opts.roster ?? [],
    models: [],
    transcript: [],
    code: [],
    citations: [],
    receipt: [],
    cost_total: 0,
    cost_tag: opts.costTag,
  };

  for (const ev of events) {
    switch (ev.type) {
      case "route":
        artifact.mode = ev.mode;
        break;
      case "model":
        if (ev.label && !artifact.models.includes(ev.label)) artifact.models.push(ev.label);
        break;
      case "answer":
        artifact.transcript.push({ pane: ev.pane, title: ev.title, text: ev.text });
        break;
      case "code":
        artifact.code.push({ language: ev.language, source: ev.source });
        break;
      case "citation":
        artifact.citations.push({
          source: ev.source,
          modality: ev.modality,
          ref: ev.ref,
          thumb: ev.thumb,
        });
        break;
      case "divergence":
        artifact.divergence = { summary: ev.summary, claims: ev.claims };
        break;
      case "cost":
        artifact.cost_total = ev.total;
        break;
      case "receipt":
        artifact.receipt = ev.rows;
        artifact.cost_total = ev.total;
        break;
      default:
        break; // unknown events ignored (forward-compatible)
    }
  }
  return artifact;
}

// Render the receipt as CSV, tagged for chargeback (§10.2.8). Mirrors the Python
// receipt_to_csv: header + one row per line + a trailing TOTAL row.
export function receiptToCsv(artifact: RunArtifact): string {
  const tag = artifact.cost_tag ?? "";
  const esc = (v: string) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  const lines = ["run_id,cost_tag,label,kind,cost"];
  for (const row of artifact.receipt) {
    lines.push(
      [artifact.run_id, tag, row.label, row.kind, row.cost.toFixed(6)].map(esc).join(","),
    );
  }
  lines.push([artifact.run_id, tag, "TOTAL", "", artifact.cost_total.toFixed(6)].map(esc).join(","));
  return lines.join("\n") + "\n";
}
