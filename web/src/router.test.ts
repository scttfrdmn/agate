import { describe, expect, it } from "vitest";

import {
  AUTO,
  entitledModels,
  modelOptions,
  resolveMode,
  resolveModelPin,
  routeToUi,
  uiToRoute,
  UI_MODES,
} from "./router";

describe("mode mapping", () => {
  it("maps UI modes to the wire vocabulary and back", () => {
    expect(uiToRoute("ask")).toBe("SYNTHESIS");
    expect(uiToRoute("panel")).toBe("DEBATE");
    expect(uiToRoute("analyze")).toBe("ANALYSIS");
    expect(routeToUi("SYNTHESIS")).toBe("ask");
    expect(routeToUi("DEBATE")).toBe("panel");
    expect(routeToUi("ANALYSIS")).toBe("analyze");
  });

  it("is a round-trip", () => {
    for (const m of UI_MODES) {
      expect(routeToUi(uiToRoute(m.value))).toBe(m.value);
    }
  });
});

describe("resolveMode (override precedence)", () => {
  it("override wins over the routed default", () => {
    expect(resolveMode("SYNTHESIS", "panel")).toBe("DEBATE");
    expect(resolveMode("ANALYSIS", "ask")).toBe("SYNTHESIS");
  });

  it("keeps the routed mode with no override", () => {
    expect(resolveMode("DEBATE", null)).toBe("DEBATE");
    expect(resolveMode("DEBATE", undefined)).toBe("DEBATE");
  });
});

describe("UI_MODES", () => {
  it("lists modes cheapest-first (ask before panel before analyze)", () => {
    expect(UI_MODES.map((m) => m.value)).toEqual(["ask", "panel", "analyze"]);
  });
});

describe("model axis (#122) — entitlement-aware", () => {
  // Lockstep with agate.entitlements.models_for_tier (cumulative, cheapest-first).
  // If this breaks, the SPA picker has drifted from the Python entitlement table.
  it("entitledModels is cumulative and cheapest-first", () => {
    expect(entitledModels("oss")).toEqual([
      "openai.gpt-oss-20b-1:0",
      "openai.gpt-oss-120b-1:0",
      "google.gemma-3-12b-it",
      "google.gemma-3-4b-it",
    ]);
    // mid includes all of oss, then the mid models
    expect(entitledModels("mid").slice(0, 4)).toEqual(entitledModels("oss"));
    expect(entitledModels("mid")).toContain("us.anthropic.claude-haiku-4-5-20251001-v1:0");
    // frontier includes everything
    expect(entitledModels("frontier")).toContain("us.anthropic.claude-opus-4-1-20250805-v1:0");
    expect(entitledModels("frontier").length).toBeGreaterThan(entitledModels("mid").length);
  });

  it("resolveModelPin honours an entitled pin and drops everything else (fail-closed)", () => {
    const oss = entitledModels("oss");
    expect(resolveModelPin(oss[1], oss)).toBe(oss[1]); // in-tier pin wins
    expect(resolveModelPin(AUTO, oss)).toBeNull(); // auto -> no pin
    expect(resolveModelPin(null, oss)).toBeNull();
    // a frontier model is NOT in an oss session's entitled set -> dropped
    expect(resolveModelPin("us.anthropic.claude-opus-4-1-20250805-v1:0", oss)).toBeNull();
  });

  it("modelOptions lists Auto first, then the entitled models", () => {
    const opts = modelOptions("oss");
    expect(opts[0].value).toBe(AUTO);
    expect(opts.slice(1).map((o) => o.value)).toEqual(entitledModels("oss"));
  });
});
