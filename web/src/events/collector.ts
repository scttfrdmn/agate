// A transport-agnostic event sink + a pure reducer from the event stream to the
// render state the SPA panes consume (§10.2.9). Kept pure and framework-free so it
// unit-tests without a browser and can also back a CLI runner.

import type {
  DivergencePayload,
  Emit,
  RouteMode,
  RunEvent,
} from "./protocol";

// Collect events in order — the test collector and "save run" serialiser both use
// this. `emit` satisfies the Emit contract.
export class EventCollector {
  readonly events: RunEvent[] = [];
  readonly emit: Emit = (event) => {
    this.events.push(event);
  };

  ofType<T extends RunEvent["type"]>(type: T): Extract<RunEvent, { type: T }>[] {
    return this.events.filter((e) => e.type === type) as Extract<
      RunEvent,
      { type: T }
    >[];
  }
}

// --- Render state ----------------------------------------------------------

export interface PaneState {
  label: string;
  tier: string;
  state: "start" | "done";
  text: string;
  elapsed_s?: number;
  cost?: number;
  usage?: { inputTokens: number; outputTokens: number };
}

export interface AnalyzeCell {
  language: string;
  source: string;
  chart?: { mime: string; data: string };
}

export interface RunState {
  mode?: RouteMode;
  // Panel columns, keyed by pane label, in first-seen order.
  panes: PaneState[];
  // Single-stream (Ask) answer text, when not scoped to a pane.
  answer: string;
  divergence?: DivergencePayload;
  cells: AnalyzeCell[];
  costTotal: number;
  artifactUrl?: string;
}

function emptyState(): RunState {
  return { panes: [], answer: "", cells: [], costTotal: 0 };
}

function paneFor(state: RunState, label: string, tier: string): PaneState {
  let pane = state.panes.find((p) => p.label === label);
  if (!pane) {
    pane = { label, tier, state: "start", text: "" };
    state.panes.push(pane);
  }
  return pane;
}

// Fold one event into the run state. Pure: returns a new state, never mutates the
// input. Unknown event types pass through untouched (forward-compatible).
export function reduce(prev: RunState, event: RunEvent): RunState {
  const state: RunState = {
    ...prev,
    panes: prev.panes.map((p) => ({ ...p })),
    cells: prev.cells.map((c) => ({ ...c })),
  };

  switch (event.type) {
    case "route":
      state.mode = event.mode;
      return state;

    case "model": {
      const pane = paneFor(state, event.label, event.tier);
      pane.state = event.state;
      if (event.elapsed_s !== undefined) pane.elapsed_s = event.elapsed_s;
      if (event.cost !== undefined) pane.cost = event.cost;
      if (event.usage) pane.usage = event.usage;
      return state;
    }

    case "answer":
      if (event.pane) {
        paneFor(state, event.pane, "").text += event.text;
      } else {
        state.answer += event.text;
      }
      return state;

    case "divergence":
      state.divergence = { summary: event.summary, claims: event.claims };
      return state;

    case "code":
      state.cells.push({ language: event.language, source: event.source });
      return state;

    case "chart": {
      const cell = state.cells[state.cells.length - 1];
      if (cell) cell.chart = { mime: event.mime, data: event.data };
      else state.cells.push({ language: "python", source: "", chart: { mime: event.mime, data: event.data } });
      return state;
    }

    case "cost":
      state.costTotal = event.total;
      return state;

    case "artifact":
      state.artifactUrl = event.url;
      return state;

    // citation / receipt / guardrail / policy_denied don't change pane layout
    // state here; the SPA renders them from the raw event list.
    default:
      return state;
  }
}

// Fold an ordered event list into final render state (used by "save run" and tests).
export function runStateFrom(events: RunEvent[]): RunState {
  return events.reduce(reduce, emptyState());
}

export { emptyState as emptyRunState };
