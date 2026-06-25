// Mode router — SPA side (§10.2.2). The default mode comes from the server-side
// routing call (a `route` event); the user can force a mode and the override wins.
// This module is the pure mapping + precedence the UI uses; it makes no model call
// (the cheap routing call is server-side, agate/router.py).

import type { RouteMode } from "./events/protocol";

// User-facing mode labels <-> the on-the-wire RouteMode vocabulary.
export type UiMode = "ask" | "panel" | "analyze";

const UI_TO_ROUTE: Record<UiMode, RouteMode> = {
  ask: "SYNTHESIS",
  panel: "DEBATE",
  analyze: "ANALYSIS",
};

const ROUTE_TO_UI: Record<RouteMode, UiMode> = {
  SYNTHESIS: "ask",
  DEBATE: "panel",
  ANALYSIS: "analyze",
};

export function uiToRoute(mode: UiMode): RouteMode {
  return UI_TO_ROUTE[mode];
}

export function routeToUi(mode: RouteMode): UiMode {
  return ROUTE_TO_UI[mode];
}

// Precedence: an explicit user override wins over the routed default; an absent or
// invalid override keeps the routed mode. Mirrors agate/router.resolve_mode.
export function resolveMode(routed: RouteMode, override?: UiMode | null): RouteMode {
  if (override && override in UI_TO_ROUTE) return UI_TO_ROUTE[override];
  return routed;
}

// The selectable modes for an override control, in cost order (cheapest first).
export const UI_MODES: ReadonlyArray<{ value: UiMode; label: string }> = [
  { value: "ask", label: "Ask" },
  { value: "panel", label: "Panel" },
  { value: "analyze", label: "Analyze" },
];

// --- model axis (#122): entitlement-aware auto mode -------------------------
// The SPA's model picker. Default is "Auto" (the server-side router picks within the
// session's entitled+affordable set); the user may PIN any ENTITLED model. A pin
// outside entitlement is never offered (the picker only lists the tier's models) and
// dropped defensively by resolveModelPin — fail-closed, mirrors agate.router.resolve_model.

export type Tier = "oss" | "mid" | "frontier";
export const AUTO = "auto";

// Tier -> entitled model ids, cheapest-first. MUST stay in lockstep with
// agate.entitlements.TIER_MODELS / models_for_tier (cumulative: a tier includes all
// lower tiers). A parity test guards this against drift.
const TIER_MODELS: Record<Tier, readonly string[]> = {
  oss: [
    "openai.gpt-oss-20b-1:0",
    "openai.gpt-oss-120b-1:0",
    "google.gemma-3-12b-it",
    "google.gemma-3-4b-it",
  ],
  mid: [
    "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
  ],
  frontier: [
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-opus-4-1-20250805-v1:0",
  ],
};

const TIER_RANK: Record<Tier, number> = { oss: 0, mid: 1, frontier: 2 };

// All models a session at `tier` may invoke (cumulative, cheapest-first) — the picker's
// options and the allow-set resolveModelPin clamps a pin to.
export function entitledModels(tier: Tier): string[] {
  const rank = TIER_RANK[tier];
  const out: string[] = [];
  for (const t of ["oss", "mid", "frontier"] as Tier[]) {
    if (TIER_RANK[t] <= rank) out.push(...TIER_MODELS[t]);
  }
  return out;
}

// A pin wins only if it's in the entitled set; "auto"/absent/unentitled -> no pin (the
// server-side router decides). Mirrors agate.router.resolve_model precedence.
export function resolveModelPin(pin: string | null | undefined, entitled: string[]): string | null {
  if (pin && pin !== AUTO && entitled.includes(pin)) return pin;
  return null;
}

// The selectable options for the model picker: "Auto" first, then each entitled model.
export function modelOptions(tier: Tier): ReadonlyArray<{ value: string; label: string }> {
  return [
    { value: AUTO, label: "Auto (entitlement-aware)" },
    ...entitledModels(tier).map((m) => ({ value: m, label: modelLabel(m) })),
  ];
}

// Approximate context window (in tokens) for a model id, for the UI's context-usage
// indicator. These are conservative published figures; the server is the authority
// on actual limits — this only drives a "how full is the context" gauge. Pure.
export function contextWindow(modelId: string): number {
  const id = modelId.toLowerCase();
  if (id.includes("claude")) return 200_000;
  if (id.includes("gpt-oss")) return 128_000;
  if (id.includes("gemma")) return 8_192;
  return 128_000; // sensible default
}

// A short human label for a model id (the wire id is long + provider-prefixed).
// Used in the model picker and the answer's "which model replied" line. Pure.
export function modelLabel(modelId: string): string {
  const id = modelId.replace(/^[a-z-]+\./, "").replace(/-v\d+:\d+$/, "");
  const map: Record<string, string> = {
    "gpt-oss-20b-1:0": "GPT-OSS 20B",
    "gpt-oss-120b-1:0": "GPT-OSS 120B",
    "gemma-3-12b-it": "Gemma 3 12B",
    "gemma-3-4b-it": "Gemma 3 4B",
  };
  if (map[modelId.replace(/^[a-z-]+\./, "")]) return map[modelId.replace(/^[a-z-]+\./, "")];
  // anthropic claude ids -> "Claude Haiku 4.5" etc.
  const claude = /claude-(opus|sonnet|haiku)-(\d+)-(\d+)/i.exec(modelId);
  if (claude) {
    const fam = claude[1][0].toUpperCase() + claude[1].slice(1);
    return `Claude ${fam} ${claude[2]}.${claude[3]}`;
  }
  return id;
}
