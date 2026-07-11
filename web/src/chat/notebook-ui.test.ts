// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import type { CellKind, Notebook } from "./notebook";
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
        { id: "a", kind: "prompt", prompt: "one?", state: "idle" },
        { id: "b", kind: "prompt", prompt: "two?", state: "idle" },
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
    const nb: Notebook = { cells: [{ id: "a", kind: "prompt", prompt: "orig", state: "idle" }] };
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
          kind: "prompt",
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
      cells: [{ id: "a", kind: "prompt", prompt: "q?", state: "error", error: "boom" }],
    };
    const target = host();
    renderNotebook(nb, target);
    const err = target.querySelector(".error-msg");
    expect(err?.textContent).toContain("boom");
  });

  it("+ Prompt and + Code fire onAddCell with the right kind", () => {
    const nb: Notebook = { cells: [{ id: "a", kind: "prompt", prompt: "q?", state: "idle" }] };
    const target = host();
    const kinds: CellKind[] = [];
    renderNotebook(nb, target, { onAddCell: (k) => kinds.push(k) });
    target.querySelector<HTMLButtonElement>(".notebook-add:not(.notebook-add-code)")!.click();
    target.querySelector<HTMLButtonElement>(".notebook-add-code")!.click();
    expect(kinds).toEqual(["prompt", "code"]);
  });

  it("namespaces citation source ids per cell so they don't collide", () => {
    const chunk = { key: "k", text: "some source text" };
    const nb: Notebook = {
      cells: [
        { id: "a", kind: "prompt", prompt: "q1", answer: "see [1]", state: "idle", sources: [chunk] },
        { id: "b", kind: "prompt", prompt: "q2", answer: "see [1]", state: "idle", sources: [chunk] },
      ],
    };
    const target = host();
    renderNotebook(nb, target);
    const ids = Array.from(target.querySelectorAll(".source-item")).map((li) => li.id);
    expect(ids).toEqual(["a-cite-1", "b-cite-1"]); // per-cell prefix, no collision
  });

  it("renders a code cell with a disabled Run and no transport wiring", () => {
    const nb: Notebook = {
      cells: [{ id: "c", kind: "code", prompt: "print('hi')", state: "idle" }],
    };
    const target = host();
    let ran = false;
    renderNotebook(nb, target, { onRun: () => (ran = true) });
    const cell = target.querySelector<HTMLElement>('.notebook-cell[data-kind="code"]')!;
    expect(cell).not.toBeNull();
    const src = cell.querySelector<HTMLTextAreaElement>(".notebook-cell-code-src")!;
    expect(src.value).toBe("print('hi')");
    const run = cell.querySelector<HTMLButtonElement>(".notebook-cell-run")!;
    expect(run.disabled).toBe(true);
    run.click(); // disabled + inert — must not fire onRun
    expect(ran).toBe(false);
  });
});
