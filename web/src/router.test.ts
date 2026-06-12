import { describe, expect, it } from "vitest";

import { resolveMode, routeToUi, uiToRoute, UI_MODES } from "./router";

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
