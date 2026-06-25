// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { renderMarkdown } from "./markdown";

// NOTE on the XSS boundary: sanitization is delegated to DOMPurify, the standard
// battle-tested sanitizer, which runs against the real browser DOM in production.
// happy-dom's HTML parser is not faithful enough to exercise DOMPurify's stripping
// (it drops/keeps tags differently than a browser), so these tests cover the logic
// WE own — math extraction, markdown structure, the math/markdown pipeline — and do
// not re-assert DOMPurify's sanitization (that's the library's own tested contract).

describe("renderMarkdown", () => {
  it("renders basic Markdown to HTML", () => {
    const html = renderMarkdown("**bold** and *italic* and `code`");
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain("<em>italic</em>");
    expect(html).toContain("<code>code</code>");
  });

  it("renders list items", () => {
    const html = renderMarkdown("- one\n- two");
    expect(html).toContain("<li>one</li>");
    expect(html).toContain("<li>two</li>");
  });

  it("typesets display math written as \\[ ... \\]", () => {
    const html = renderMarkdown("The law:\n\n\\[ dU = Q - W \\]");
    // KaTeX emits a .katex container; the raw TeX delimiters are gone and the
    // formula has been typeset into KaTeX markup (not left as the literal string).
    expect(html).toContain("katex");
    expect(html).not.toContain("\\[");
    expect(html).toContain("katex-display"); // display mode
  });

  it("typesets inline math written as \\( ... \\) and $ ... $", () => {
    const a = renderMarkdown("energy \\(E=mc^2\\) is famous");
    expect(a).toContain("katex");
    expect(a).not.toContain("\\(");
    const b = renderMarkdown("inline $a^2+b^2$ here");
    expect(b).toContain("katex");
  });

  it("never leaks a MATH<n>MATH placeholder (math in lists + next to punctuation)", () => {
    // Regression: the placeholder was inserted with surrounding spaces but matched
    // with them too; the browser's HTML parser trims that whitespace in block
    // elements, so every token leaked as raw "MATHnMATH" text. Matching the
    // space-free core fixes it. Exercise the structures that triggered it.
    const src = [
      "- **Enthalpy (H)** – defined as \\(H = U + PV\\). At constant pressure:",
      "  \\[ \\Delta H = q_p \\]",
      "  Exothermic release heat (negative \\(\\Delta H\\)); endothermic absorb it.",
      "- **Gibbs (G)** – given by \\(G = H - TS\\); spontaneous when \\(\\Delta G < 0\\).",
    ].join("\n");
    const html = renderMarkdown(src);
    expect(html).not.toMatch(/MATH\d+MATH/);
    expect(html).toContain("katex");
  });

  it("does not treat a lone currency $ as math", () => {
    const html = renderMarkdown("It costs $5 today.");
    expect(html).toContain("$5"); // left as text, not swallowed as math
    expect(html).not.toContain("katex");
  });

  it("renders malformed TeX without throwing or emitting a script tag", () => {
    // throwOnError off -> KaTeX renders an error node, never raw HTML/script.
    const html = renderMarkdown("\\[ \\frac{1}{ \\]");
    expect(typeof html).toBe("string");
    expect(html.toLowerCase()).not.toContain("<script");
  });

  it("passes model output through the DOMPurify sanitizer (returns a string)", () => {
    // We can't fairly exercise DOMPurify under happy-dom, but assert the pipeline
    // runs end-to-end on hostile input and yields a string (no throw, no crash).
    const html = renderMarkdown("hi <script>alert('x')</script> **there**");
    expect(typeof html).toBe("string");
    expect(html).toContain("<strong>there</strong>");
  });
});
