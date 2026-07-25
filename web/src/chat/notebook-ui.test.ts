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

  it("renders an answered prompt cell in the CHAT costume (read-only turn + one receipt)", () => {
    // Two renderers, one model (#242): an answered, un-referenced, un-expanded prompt cell is a
    // chat turn, NOT an editable cell — question bubble + rendered answer + a single receipt.
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
    // Chat costume: a msg-pair with the question verbatim + rendered markdown, no editable textarea.
    expect(target.querySelector(".notebook-cell-chat .msg-pair")).not.toBeNull();
    expect(target.querySelector(".q-text")?.textContent).toBe("q?");
    const body = target.querySelector(".answer-body")!;
    expect(body.querySelector("strong")?.textContent).toBe("bold");
    expect(target.querySelector(".notebook-cell-prompt")).toBeNull(); // no cell chrome yet
    // Exactly one receipt per turn (the Phase-1 rule).
    expect(target.querySelectorAll(".msg-receipt")).toHaveLength(1);
  });

  it("Edit grows a chat turn into an editable cell; the answered cell offers Edit + Re-run", () => {
    const nb: Notebook = {
      cells: [{ id: "a", kind: "prompt", prompt: "q?", answer: "an answer", state: "idle" }],
    };
    const target = host();
    const expands: Array<[string, boolean]> = [];
    const reruns: string[] = [];
    renderNotebook(nb, target, {
      onToggleExpand: (id, e) => expands.push([id, e]),
      onRun: (id) => reruns.push(id),
    });
    const actions = [...target.querySelectorAll<HTMLButtonElement>(".turn-action")];
    const edit = actions.find((b) => b.textContent === "Edit")!;
    const rerun = actions.find((b) => b.textContent === "Re-run")!;
    edit.click();
    rerun.click();
    expect(expands).toEqual([["a", true]]);
    expect(reruns).toEqual(["a"]);
  });

  it("an expanded prompt cell shows the editable textarea + a Done (collapse) control", () => {
    const nb: Notebook = {
      cells: [{ id: "a", kind: "prompt", prompt: "q?", answer: "an answer", state: "idle", expanded: true }],
    };
    const target = host();
    const collapses: Array<[string, boolean]> = [];
    renderNotebook(nb, target, { onToggleExpand: (id, e) => collapses.push([id, e]) });
    expect(target.querySelector(".notebook-cell-prompt")).not.toBeNull();
    const done = target.querySelector<HTMLButtonElement>(".notebook-cell-collapse")!;
    expect(done).not.toBeNull();
    done.click();
    expect(collapses).toEqual([["a", false]]);
  });

  it("a stale answered cell shows cell chrome + a stale badge + a Cancel (never a bare chat turn)", () => {
    // Data-integrity: once edited-since-run (stale), the frozen answer must NOT be re-presented as
    // an authoritative chat turn — it stays a cell with the stale badge and a reversible Cancel.
    const nb: Notebook = {
      cells: [
        {
          id: "a",
          kind: "prompt",
          prompt: "edited q?",
          answer: "old answer",
          answeredPrompt: "original q?",
          state: "idle",
          expanded: true,
          stale: true,
        },
      ],
    };
    const target = host();
    renderNotebook(nb, target);
    expect(target.querySelector(".notebook-cell-chat")).toBeNull(); // not a chat turn
    expect(target.querySelector(".notebook-cell-prompt")).not.toBeNull();
    expect(target.querySelector(".notebook-cell-stale")).not.toBeNull();
    const cancel = target.querySelector<HTMLButtonElement>(".notebook-cell-collapse")!;
    expect(cancel.textContent).toBe("Cancel");
  });

  it("an unanswered prompt cell always shows cell chrome (it IS the working cell)", () => {
    const nb: Notebook = { cells: [{ id: "a", kind: "prompt", prompt: "draft", state: "idle" }] };
    const target = host();
    renderNotebook(nb, target);
    expect(target.querySelector(".notebook-cell-prompt")).not.toBeNull();
    expect(target.querySelector(".notebook-cell-chat")).toBeNull();
  });

  it("a referenced prompt cell reveals its cell chrome (its {{cN}} output is load-bearing)", () => {
    const nb: Notebook = {
      cells: [
        { id: "a", name: "c1", kind: "prompt", prompt: "q1", answer: "A1", state: "idle" },
        { id: "b", name: "c2", kind: "prompt", prompt: "use {{c1}}", answer: "A2", state: "idle" },
      ],
    };
    const target = host();
    renderNotebook(nb, target);
    // c1 is referenced by c2 → c1 shows as an editable cell; c2 (not referenced) stays chat.
    expect(target.querySelector('.notebook-cell[data-cell-id="a"] .notebook-cell-prompt')).not.toBeNull();
    expect(target.querySelector('.notebook-cell-chat[data-cell-id="b"]')).not.toBeNull();
  });

  it("a Run button on a python block in an answer fires onRunFromAnswer with the cell id + code (#243)", () => {
    const nb: Notebook = {
      cells: [
        {
          id: "a",
          kind: "prompt",
          prompt: "plot it",
          answer: "Here you go:\n\n```python\nimport matplotlib\n```",
          state: "idle",
        },
      ],
    };
    const target = host();
    const runs: Array<[string, string]> = [];
    renderNotebook(nb, target, { onRunFromAnswer: (id, code) => runs.push([id, code]) });
    const run = target.querySelector<HTMLButtonElement>(".code-run");
    expect(run).not.toBeNull();
    run!.click();
    expect(runs).toHaveLength(1);
    expect(runs[0][0]).toBe("a");
    expect(runs[0][1]).toContain("import matplotlib");
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

  it("has no persistent +Prompt/+Code add-bar (the composer is the append path, #242)", () => {
    const nb: Notebook = { cells: [{ id: "a", kind: "prompt", prompt: "q?", state: "idle" }] };
    const target = host();
    renderNotebook(nb, target);
    expect(target.querySelector(".notebook-add-bar")).toBeNull();
    expect(target.querySelector(".notebook-add")).toBeNull();
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

  it("renders Sources as a collapsible <details open> with a web source as a link", () => {
    const nb: Notebook = {
      cells: [
        {
          id: "a",
          kind: "prompt",
          prompt: "q",
          answer: "see [1]",
          state: "idle",
          sources: [
            { key: "k", text: "corpus text" },
            { key: "w", text: "web text", sourceSystem: "web", sourceItem: "https://example.edu/paper" },
          ],
        },
      ],
    };
    const target = host();
    renderNotebook(nb, target);
    const details = target.querySelector("details.sources") as HTMLDetailsElement;
    expect(details).not.toBeNull();
    expect(details.open).toBe(true); // default open so citation anchors resolve
    expect(target.querySelector("summary.sources-title")?.textContent).toContain("Sources (2)");
    const link = target.querySelector<HTMLAnchorElement>("a.source-link");
    expect(link?.href).toBe("https://example.edu/paper");
    expect(link?.rel).toContain("noopener");
  });

  it("renders a code cell whose Run fires onRunCode with the edited source", () => {
    const nb: Notebook = {
      cells: [{ id: "c", kind: "code", prompt: "print('hi')", state: "idle" }],
    };
    const target = host();
    const calls: Array<[string, string]> = [];
    renderNotebook(nb, target, { onRunCode: (id, code) => calls.push([id, code]) });
    const cell = target.querySelector<HTMLElement>('.notebook-cell[data-kind="code"]')!;
    expect(cell).not.toBeNull();
    const src = cell.querySelector<HTMLTextAreaElement>(".notebook-cell-code-src")!;
    expect(src.value).toBe("print('hi')");
    src.value = "print('bye')";
    const run = cell.querySelector<HTMLButtonElement>(".notebook-cell-run")!;
    expect(run.disabled).toBe(false);
    run.click();
    expect(calls).toEqual([["c", "print('bye')"]]);
  });

  it("renders code output: stdout, last-expr value, and errors", () => {
    const nb: Notebook = {
      cells: [
        {
          id: "c",
          kind: "code",
          prompt: "x=1\nx+1",
          state: "idle",
          output: { stdout: "log line\n", stderr: "", result: "2" },
        },
        {
          id: "e",
          kind: "code",
          prompt: "boom",
          state: "error",
          output: { stdout: "", stderr: "", error: "Traceback: NameError: boom" },
        },
      ],
    };
    const target = host();
    renderNotebook(nb, target);
    expect(target.querySelector(".notebook-code-stdout")?.textContent).toBe("log line");
    expect(target.querySelector(".notebook-code-result")?.textContent).toBe("2");
    expect(target.querySelector(".notebook-code-error")?.textContent).toContain("NameError");
  });

  it("renders matplotlib figure PNGs and rejects non-PNG data URIs", () => {
    const png = "data:image/png;base64,iVBORw0KGgo=";
    const nb: Notebook = {
      cells: [
        {
          id: "c",
          kind: "code",
          prompt: "plot",
          state: "idle",
          output: {
            stdout: "",
            stderr: "",
            images: [png, "javascript:alert(1)", "data:text/html;base64,x"],
          },
        },
      ],
    };
    const target = host();
    renderNotebook(nb, target);
    const imgs = target.querySelectorAll<HTMLImageElement>(".notebook-code-image");
    expect(imgs).toHaveLength(1); // only the valid PNG data URI is rendered
    expect(imgs[0].src).toBe(png);
  });

  it("shows a running/loading state for a code cell in flight", () => {
    const nb: Notebook = {
      cells: [{ id: "c", kind: "code", prompt: "1", state: "running", error: "Downloading…" }],
    };
    const target = host();
    renderNotebook(nb, target);
    const run = target.querySelector<HTMLButtonElement>(".notebook-cell-run")!;
    expect(run.disabled).toBe(true);
    expect(target.querySelector(".notebook-code-loading")?.textContent).toBe("Downloading…");
  });

  it("shows the {{cN}} reference name and a stale badge (#200 slice 3)", () => {
    const nb: Notebook = {
      cells: [
        { id: "a", name: "c1", kind: "prompt", prompt: "q", answer: "a", state: "idle" },
        { id: "b", name: "c2", kind: "prompt", prompt: "from {{c1}}", state: "idle", stale: true },
      ],
    };
    const target = host();
    renderNotebook(nb, target);
    const names = Array.from(target.querySelectorAll(".notebook-cell-name")).map((n) => n.textContent);
    expect(names).toEqual(["c1", "c2"]);
    // Only the stale cell shows the badge + wrapper class.
    expect(target.querySelectorAll(".notebook-cell-stale")).toHaveLength(1);
    expect(target.querySelector('.notebook-cell[data-cell-id="b"]')?.classList.contains("stale")).toBe(
      true,
    );
  });

  it("fires onEdit when a cell's source changes", () => {
    const nb: Notebook = { cells: [{ id: "a", name: "c1", kind: "prompt", prompt: "x", state: "idle" }] };
    const target = host();
    const edits: Array<[string, string]> = [];
    renderNotebook(nb, target, { onEdit: (id, s) => edits.push([id, s]) });
    const ta = target.querySelector<HTMLTextAreaElement>(".notebook-cell-prompt")!;
    ta.value = "x2";
    ta.dispatchEvent(new Event("input"));
    expect(edits).toEqual([["a", "x2"]]);
  });
});
