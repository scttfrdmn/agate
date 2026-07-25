// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";

import { renderInto } from "./markdown";

afterEach(() => {
  document.body.innerHTML = "";
});

function host(): HTMLElement {
  const el = document.createElement("div");
  document.body.appendChild(el);
  return el;
}

describe("renderInto — Run this code actions (#243)", () => {
  it("adds a Copy button but NO Run button when onRunCode is absent", () => {
    const el = host();
    renderInto(el, "```python\nprint(1)\n```");
    expect(el.querySelector(".code-copy")).not.toBeNull();
    expect(el.querySelector(".code-run")).toBeNull();
  });

  it("adds a Run button on a python block when onRunCode is provided; it fires with the code", () => {
    const el = host();
    const runs: string[] = [];
    renderInto(el, "```python\nprint('hi')\n```", "", { onRunCode: (c) => runs.push(c) });
    const run = el.querySelector<HTMLButtonElement>(".code-run");
    expect(run).not.toBeNull();
    run!.click();
    expect(runs).toHaveLength(1);
    expect(runs[0]).toContain("print('hi')");
  });

  it("does NOT add Run to a non-python block (e.g. bash), but still adds Copy", () => {
    const el = host();
    renderInto(el, "```bash\nls -la\n```", "", { onRunCode: () => {} });
    expect(el.querySelector(".code-run")).toBeNull();
    expect(el.querySelector(".code-copy")).not.toBeNull();
  });

  it("does NOT treat a bare (no-language) fence as runnable — output dumps aren't code", () => {
    const el = host();
    renderInto(el, "```\nx = 1\n```", "", { onRunCode: () => {} });
    expect(el.querySelector(".code-run")).toBeNull();
    expect(el.querySelector(".code-copy")).not.toBeNull();
  });

  it("disables the Run button after one click (no duplicate spawns)", () => {
    const el = host();
    let n = 0;
    renderInto(el, "```python\nprint(1)\n```", "", { onRunCode: () => n++ });
    const run = el.querySelector<HTMLButtonElement>(".code-run")!;
    run.click();
    run.click();
    expect(n).toBe(1); // second click is a no-op (disabled)
    expect(run.disabled).toBe(true);
  });

  it("does not add a Run button to an inline `code` span", () => {
    const el = host();
    renderInto(el, "use `print()` inline", "", { onRunCode: () => {} });
    expect(el.querySelector(".code-run")).toBeNull();
  });

  it("adds Run to each python block when there are several", () => {
    const el = host();
    renderInto(el, "```python\na=1\n```\n\ntext\n\n```python\nb=2\n```", "", { onRunCode: () => {} });
    expect(el.querySelectorAll(".code-run")).toHaveLength(2);
  });
});
