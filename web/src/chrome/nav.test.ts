// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";

import { mountChrome } from "./nav";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("mountChrome", () => {
  it("builds a top bar with an accessible nav toggle", () => {
    const { topbar } = mountChrome({ brand: "agate", items: [{ label: "Ask", href: "#" }] });
    const toggle = topbar.querySelector(".nav-toggle")!;
    expect(toggle.getAttribute("aria-controls")).toBe("agate-nav-drawer");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(topbar.getAttribute("role")).toBe("banner");
  });

  it("renders a labelled dialog drawer, hidden by default", () => {
    mountChrome({ brand: "agate", items: [{ label: "Ask", href: "#" }] });
    const drawer = document.getElementById("agate-nav-drawer")!;
    expect(drawer.getAttribute("role")).toBe("dialog");
    expect(drawer.getAttribute("aria-modal")).toBe("true");
    expect(drawer.hidden).toBe(true);
  });

  it("opens on toggle click and sets aria-expanded", () => {
    const { topbar } = mountChrome({ brand: "agate", items: [{ label: "Ask", href: "#" }] });
    const toggle = topbar.querySelector(".nav-toggle") as HTMLButtonElement;
    toggle.click();
    const drawer = document.getElementById("agate-nav-drawer")!;
    expect(drawer.hidden).toBe(false);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });

  it("fires onSelect for an item and marks current", () => {
    let chosen = "";
    mountChrome({
      brand: "agate",
      items: [
        { label: "Ask", href: "#", current: true, onSelect: () => (chosen = "ask") },
        { label: "Panel", href: "#", onSelect: () => (chosen = "panel") },
      ],
    });
    const drawer = document.getElementById("agate-nav-drawer")!;
    const current = drawer.querySelector('[aria-current="page"]')!;
    expect(current.textContent).toContain("Ask");
    (drawer.querySelectorAll("a")[1] as HTMLAnchorElement).click();
    expect(chosen).toBe("panel");
  });

  it("closes on Escape", () => {
    const { topbar } = mountChrome({ brand: "agate", items: [{ label: "Ask", href: "#" }] });
    (topbar.querySelector(".nav-toggle") as HTMLButtonElement).click();
    const toggle = topbar.querySelector(".nav-toggle") as HTMLButtonElement;
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });
});
