// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import type { Notebook } from "./notebook";
import { renderNotebook } from "./notebook-ui";

function host(): HTMLElement {
  const el = document.createElement("div");
  document.body.appendChild(el);
  return el;
}

describe("renderNotebook", () => {
  it("renders one editable textarea per cell seeded with the prompt", () => {
    const nb: Notebook = {
      cells: [
        { id: "a", prompt: "one?", state: "idle" },
        { id: "b", prompt: "two?", state: "idle" },
      ],
    };
    const target = host();
    renderNotebook(nb, target);
    const areas = target.querySelectorAll<HTMLTextAreaElement>(".notebook-cell-prompt");
    expect(areas).toHaveLength(2);
    expect([areas[0].value, areas[1].value]).toEqual(["one?", "two?"]);
    // per-cell ids don't collide
    expect(areas[0].id).not.toBe(areas[1].id);
  });

  it("Run fires onRun with the (possibly edited) prompt + cell id", () => {
    const nb: Notebook = { cells: [{ id: "a", prompt: "orig", state: "idle" }] };
    const target = host();
    const calls: Array<[string, string]> = [];
    renderNotebook(nb, target, { onRun: (id, p) => calls.push([id, p]) });
    const area = target.querySelector<HTMLTextAreaElement>(".notebook-cell-prompt")!;
    area.value = "edited?";
    target.querySelector<HTMLButtonElement>(".notebook-cell-run")!.click();
    expect(calls).toEqual([["a", "edited?"]]);
  });

  it("renders an answered cell as markdown + a receipt", () => {
    const nb: Notebook = {
      cells: [
        {
          id: "a",
          prompt: "q?",
          answer: "**bold** answer",
          state: "idle",
          meta: { cost: 0.0001, usage: { inputTokens: 5, outputTokens: 3 } },
        },
      ],
    };
    const target = host();
    renderNotebook(nb, target);
    const body = target.querySelector(".notebook-answer-body")!;
    expect(body.querySelector("strong")?.textContent).toBe("bold");
    expect(target.querySelector(".msg-receipt")).not.toBeNull();
  });

  it("renders an error state", () => {
    const nb: Notebook = {
      cells: [{ id: "a", prompt: "q?", state: "error", error: "boom" }],
    };
    const target = host();
    renderNotebook(nb, target);
    const err = target.querySelector(".error-msg");
    expect(err?.textContent).toContain("boom");
  });

  it("+ Cell fires onAddCell", () => {
    const nb: Notebook = { cells: [{ id: "a", prompt: "q?", state: "idle" }] };
    const target = host();
    let added = 0;
    renderNotebook(nb, target, { onAddCell: () => (added += 1) });
    target.querySelector<HTMLButtonElement>(".notebook-add")!.click();
    expect(added).toBe(1);
  });

  it("namespaces citation source ids per cell so they don't collide", () => {
    const chunk = { key: "k", text: "some source text" };
    const nb: Notebook = {
      cells: [
        { id: "a", prompt: "q1", answer: "see [1]", state: "idle", sources: [chunk] },
        { id: "b", prompt: "q2", answer: "see [1]", state: "idle", sources: [chunk] },
      ],
    };
    const target = host();
    renderNotebook(nb, target);
    const ids = Array.from(target.querySelectorAll(".source-item")).map((li) => li.id);
    expect(ids).toEqual(["a-cite-1", "b-cite-1"]); // per-cell prefix, no collision
  });
});
