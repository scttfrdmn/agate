// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { runStateFrom } from "../events/collector";
import type { RunEvent } from "../events/protocol";
import { renderCells, renderPanel } from "./render";

function host(): HTMLElement {
  return document.createElement("div");
}

describe("renderPanel", () => {
  const events: RunEvent[] = [
    { type: "model", tier: "frontier", label: "frontier", state: "done", pane: "frontier", elapsed_s: 2, cost: 0.001 },
    { type: "answer", pane: "frontier", text: "answer A" },
    { type: "model", tier: "ow", label: "open-weight-70b", state: "done", pane: "open-weight-70b", elapsed_s: 1, cost: 0.0002 },
    { type: "answer", pane: "open-weight-70b", text: "answer B" },
    {
      type: "divergence",
      summary: "Differ on magnitude.",
      claims: [
        {
          id: "c1", text: "Effect is large.", kind: "disagreement",
          positions: [
            { pane: "frontier", stance: "partial", note: "trial-dependent" },
            { pane: "open-weight-70b", stance: "supports" },
          ],
          verify: true, evidence_refs: ["DOC1"],
        },
      ],
    },
  ];

  it("renders one column per pane plus a reconciliation column", () => {
    const target = host();
    renderPanel(runStateFrom(events), target);
    const panes = target.querySelectorAll(".agate-pane");
    expect(panes).toHaveLength(2);
    expect(target.querySelector('[data-pane="frontier"] .agate-pane-body')?.textContent).toBe("answer A");
    expect(target.querySelector(".agate-divergence")).not.toBeNull();
  });

  it("exposes a11y semantics: panel group, labelled panes, done-state class", () => {
    const target = host();
    renderPanel(runStateFrom(events), target);
    const group = target.querySelector(".agate-panel")!;
    expect(group.getAttribute("role")).toBe("group");
    expect(group.getAttribute("aria-label")).toBe("Model panel");
    const pane = target.querySelector('[data-pane="frontier"]')!;
    expect(pane.getAttribute("aria-label")).toBe("Model frontier");
    expect(pane.classList.contains("done")).toBe(true); // state=done
  });

  it("renders a disagreement claim with verify flag and side-by-side positions", () => {
    const target = host();
    renderPanel(runStateFrom(events), target);
    const claim = target.querySelector(".agate-claim-disagreement")!;
    expect(claim).not.toBeNull();
    expect(claim.getAttribute("data-verify")).toBe("true");
    const positions = claim.querySelectorAll("li");
    expect(positions).toHaveLength(2);
    expect(positions[0].textContent).toContain("frontier: partial");
    expect(positions[1].textContent).toContain("open-weight-70b: supports");
    expect(claim.querySelector(".agate-claim-refs")?.textContent).toContain("DOC1");
  });
});

describe("renderCells (Analyze)", () => {
  it("renders an editable source cell and fires onRun with the edited text", () => {
    const events: RunEvent[] = [
      { type: "code", language: "python", source: "print(1)" },
    ];
    const target = host();
    let ran: string | undefined;
    renderCells(runStateFrom(events).cells, target, { onRun: (s) => (ran = s) });

    const editor = target.querySelector(".agate-cell-source") as HTMLTextAreaElement;
    expect(editor.value).toBe("print(1)");
    // the editable cell is labelled for assistive tech
    expect(target.querySelector("label[for='agate-cell-source']")).not.toBeNull();
    editor.value = "print(2)"; // user edits
    (target.querySelector(".agate-cell-run") as HTMLButtonElement).click();
    expect(ran).toBe("print(2)");
  });

  it("renders an inline chart image when present", () => {
    const events: RunEvent[] = [
      { type: "code", language: "python", source: "plot()" },
      { type: "chart", mime: "image/png", data: "QUJD" },
    ];
    const target = host();
    renderCells(runStateFrom(events).cells, target);
    const img = target.querySelector(".agate-cell-chart") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("data:image/png;base64,QUJD");
  });
});
