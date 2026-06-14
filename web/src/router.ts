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
