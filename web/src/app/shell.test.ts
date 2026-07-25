// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";

import { renderShell } from "./shell";

afterEach(() => {
  document.body.innerHTML = "";
});

function mount(): HTMLElement {
  const app = document.createElement("div");
  document.body.appendChild(app);
  renderShell(app);
  return app;
}

describe("renderShell — Canvas Phase 1 composer (#242)", () => {
  it("has no Chat/Notebook view toggle (unified surface)", () => {
    const app = mount();
    expect(app.querySelector("#view-toggle")).toBeNull();
    expect(app.querySelector(".view-btn")).toBeNull();
  });

  it("puts an Ask-weighted Code affordance inside the input bar, default Ask", () => {
    const app = mount();
    const toggle = app.querySelector("#code-toggle") as HTMLButtonElement;
    expect(toggle).not.toBeNull();
    // Lives in the input bar (the composer), not as a separate peer tab.
    expect(toggle.closest(".input-bar")).not.toBeNull();
    // Default state is Ask (not pressed) — the surface reads as "just chat" on first contact.
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
  });

  it("keeps Mode + Model as labelled, separated controls (not bare dropdowns)", () => {
    const app = mount();
    const labels = [...app.querySelectorAll(".control-field .control-label")].map(
      (el) => el.textContent,
    );
    expect(labels).toContain("Model");
    expect(labels).toContain("Mode");
    // Model is the prominent field; Mode is demoted + separated.
    expect(app.querySelector(".control-field-model #model")).not.toBeNull();
    expect(app.querySelector(".control-field-mode #mode")).not.toBeNull();
  });

  it("still renders the composer form + question box", () => {
    const app = mount();
    expect(app.querySelector("form#f.composer-bar")).not.toBeNull();
    expect(app.querySelector("textarea#q")).not.toBeNull();
    expect(app.querySelector("button.send-btn")).not.toBeNull();
  });
});
