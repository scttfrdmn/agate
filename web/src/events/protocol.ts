// The run event protocol (§10.2.9).
//
// A *run* is an ordered stream of typed events that the SPA renders live and can
// serialise into a reproducible artifact (§10.2.8). The contract is
// transport-agnostic: the same event shapes serve a WebSocket to the browser, a
// CLI runner, and a test collector. The Python orchestration (agg/panel) emits
// these; the SPA renders them; tests collect them.
//
// New in this model (additive, backward-compatible): the `pane` field on `model`,
// and the `divergence`, `citation`, and `artifact` events. Everything else mirrors
// the existing streaming chat path.

// Mode/route vocabulary on the wire (mirrors the orchestration router so existing
// code ports directly). SYNTHESIS -> Ask, DEBATE -> Panel, ANALYSIS -> Analyze.
export type RouteMode = "SYNTHESIS" | "DEBATE" | "ANALYSIS";

export type CitationModality = "text" | "image" | "table" | "audio" | "video";

// --- Adjudicator / divergence (mirrors agg/panel/schema.py and §10.2.5) ------

export type Stance = "supports" | "disputes" | "partial" | "silent";
export type ClaimKind = "agreement" | "disagreement" | "unsupported";

export interface ClaimPosition {
  pane: string; // MUST equal a roster member's label
  stance: Stance;
  note?: string;
}

export interface DivergenceClaim {
  id: string;
  text: string;
  kind: ClaimKind;
  positions: ClaimPosition[];
  verify: boolean;
  evidence_refs?: string[];
}

export interface DivergencePayload {
  summary: string;
  claims: DivergenceClaim[];
}

// --- The event union --------------------------------------------------------

export interface RouteEvent {
  type: "route";
  mode: RouteMode;
}

// A model's lifecycle within a run. `pane` maps it to a Panel column (additive:
// absent for single-stream Ask).
export interface ModelEvent {
  type: "model";
  tier: string;
  label: string;
  state: "start" | "done";
  pane?: string;
  elapsed_s?: number;
  usage?: { inputTokens: number; outputTokens: number };
  cost?: number;
}

// Streamed/atomic answer text. `pane` optionally scopes it to a Panel column.
export interface AnswerEvent {
  type: "answer";
  title?: string;
  text: string;
  pane?: string;
}

// Structured adjudicator output — drives the side-by-side divergence UI.
export type DivergenceEvent = { type: "divergence" } & DivergencePayload;

// Resolve a claim to a text passage or a specific visual element.
export interface CitationEvent {
  type: "citation";
  source: string;
  modality: CitationModality;
  ref: string;
  thumb?: string;
}

// The serialised reproducible run record (§10.2.8).
export interface ArtifactEvent {
  type: "artifact";
  run_id: string;
  url: string;
}

// Generated Python for the Analyze cell (rendered as an editable notebook cell).
export interface CodeEvent {
  type: "code";
  language: string;
  source: string;
  pane?: string;
}

// Inline chart/image result from an Analyze run.
export interface ChartEvent {
  type: "chart";
  mime: string;
  data: string; // base64 image or similar
  pane?: string;
}

// Running cost total (non-authoritative live estimate; §10.2.8, design §7.2).
export interface CostEvent {
  type: "cost";
  total: number;
}

// Itemised receipt closing a run.
export interface ReceiptRow {
  label: string;
  kind: "llm" | "compute" | "retrieval";
  cost: number;
}
export interface ReceiptEvent {
  type: "receipt";
  rows: ReceiptRow[];
  total: number;
}

// Guardrail intervention surfaced to the user.
export interface GuardrailEvent {
  type: "guardrail";
  action: string;
  detail?: string;
}

// A Cedar/tool policy denial (governance at the boundary; §10.2.8).
export interface PolicyDeniedEvent {
  type: "policy_denied";
  tool: string;
  reason?: string;
}

export type RunEvent =
  | RouteEvent
  | ModelEvent
  | AnswerEvent
  | DivergenceEvent
  | CitationEvent
  | ArtifactEvent
  | CodeEvent
  | ChartEvent
  | CostEvent
  | ReceiptEvent
  | GuardrailEvent
  | PolicyDeniedEvent;

export type RunEventType = RunEvent["type"];

// The emit contract: a sink that receives events in order. Transport-agnostic —
// a WebSocket pump, a CLI printer, or a test collector all satisfy it.
export type Emit = (event: RunEvent) => void;
