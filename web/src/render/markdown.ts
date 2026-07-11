// Markdown + math rendering for assistant answers (Ask / Panel / Analyze).
//
// The model returns GitHub-flavored Markdown that often includes LaTeX math —
// our audience is academics and researchers, so dU = Q - W should typeset, not
// show as raw `\[ ... \]`. Pipeline:
//   1. extract math spans ($$..$$, \[..\], $..$, \(..\)) BEFORE markdown parsing
//      (so marked never mangles backslashes / underscores inside formulae),
//   2. render the markdown with `marked`,
//   3. sanitize the HTML with DOMPurify (we insert via innerHTML, so this is the
//      XSS boundary — the model output is untrusted text),
//   4. typeset the placeholders with KaTeX into the sanitized DOM.
//
// KaTeX output is a fixed, safe HTML/MathML subset; we allow its classes/tags in
// the DOMPurify config and typeset after sanitizing so KaTeX's own markup isn't
// stripped. Math that fails to parse is left as the original text (throwOnError
// off), never raw HTML — so a malformed formula can't inject markup.

import { Marked } from "marked";
import createDOMPurify from "dompurify";
import katex from "katex";

// One marked instance, GFM on, no raw HTML pass-through from the model (we render
// Markdown only; any literal HTML in the answer is escaped, then DOMPurify is the
// backstop). Async extensions off so render() is synchronous.
const marked = new Marked({ gfm: true, breaks: true });

// DOMPurify's default export is a factory that needs a `window` (so it works under
// both the browser and the happy-dom test env). Bind it once to the global window.
const purify = createDOMPurify(globalThis.window ?? (globalThis as unknown as Window));

interface MathSpan {
  core: string; // space-free placeholder matched after sanitizing
  tex: string;
  display: boolean;
}

// Sentinel that survives Markdown parsing untouched (no markdown-special chars).
// Inserted with surrounding spaces so Markdown treats it as a standalone word, but
// we MATCH on the space-free `core` when swapping in KaTeX: the browser's DOMParser
// (inside DOMPurify) trims leading/trailing whitespace in block elements, so a
// spaced sentinel wouldn't match in a real browser (it matched only in the test
// DOM, which is why a leak slipped through). The core is alphanumeric, so Markdown
// and sanitizing never alter it — it matches in both environments.
const mathCore = (i: number) => "MATH" + i + "MATH";
const mathToken = (i: number) => " " + mathCore(i) + " ";

// Pull math spans out, replacing each with an inert token. Order matters: match
// the display delimiters ($$, \[..\]) before inline ($, \(..\)) so we don't split
// a display block at its inner $.
function extractMath(src: string): { text: string; spans: MathSpan[] } {
  const spans: MathSpan[] = [];
  const push = (tex: string, display: boolean): string => {
    const token = mathToken(spans.length);
    spans.push({ core: mathCore(spans.length), tex: tex.trim(), display });
    return token;
  };
  let out = src;
  // $$ ... $$ (display)
  out = out.replace(/\$\$([\s\S]+?)\$\$/g, (_m, tex) => push(tex, true));
  // \[ ... \] (display)
  out = out.replace(/\\\[([\s\S]+?)\\\]/g, (_m, tex) => push(tex, true));
  // \( ... \) (inline)
  out = out.replace(/\\\(([\s\S]+?)\\\)/g, (_m, tex) => push(tex, false));
  // $ ... $ (inline) — avoid matching $$ (already gone) and bare currency by
  // requiring a non-space adjacent to the delimiters and no newline inside.
  out = out.replace(/(?<![\\$])\$(?!\s)([^\n$]+?)(?<!\s)\$(?!\$)/g, (_m, tex) =>
    push(tex, false),
  );
  return { text: out, spans };
}

// KaTeX-rendered HTML the sanitizer must keep. KaTeX emits spans/MathML with these.
const KATEX_TAGS = [
  "math",
  "semantics",
  "mrow",
  "mi",
  "mo",
  "mn",
  "msup",
  "msub",
  "mfrac",
  "msqrt",
  "annotation",
  "mspace",
  "mtable",
  "mtr",
  "mtd",
  "munderover",
  "munder",
  "mover",
];

/**
 * Render untrusted Markdown-with-math to a sanitized HTML string ready to assign
 * to `innerHTML`. Pure (no DOM mutation of the caller's nodes); deterministic.
 */
export function renderMarkdown(src: string): string {
  const { text, spans } = extractMath(src);
  const rawHtml = marked.parse(text, { async: false }) as string;
  let safe = purify.sanitize(rawHtml, {
    ADD_TAGS: KATEX_TAGS,
    ADD_ATTR: ["aria-hidden", "encoding", "xmlns", "display"],
  });
  // Swap each math placeholder for KaTeX output. Done on the sanitized string with
  // KaTeX markup (a trusted, fixed subset) — render AFTER sanitizing so KaTeX spans
  // aren't stripped. Match the space-free core (the spaced sentinel may have had its
  // surrounding whitespace trimmed by the browser's HTML parser). A parse failure
  // renders the literal TeX as escaped text, never raw HTML.
  for (const span of spans) {
    let html: string;
    try {
      html = katex.renderToString(span.tex, {
        displayMode: span.display,
        throwOnError: false,
        output: "htmlAndMathml",
      });
    } catch {
      // Last-resort: show the original TeX as escaped text, never raw.
      html = escapeHtml(span.tex);
    }
    safe = safe.replace(span.core, html);
  }
  return safe;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/**
 * Render Markdown-with-math into a target element (sets innerHTML to the sanitized
 * result). The single place answer HTML is injected, so the XSS boundary is here.
 * After rendering, wires up affordances (citation markers, code copy buttons).
 */
export function renderInto(el: HTMLElement, src: string, idPrefix = ""): void {
  el.innerHTML = renderMarkdown(src);
  markCitations(el, idPrefix);
  addCopyButtons(el);
}

// Turn bare [n] citation markers in the rendered text into superscript anchors that
// scroll to the matching source in the answer's Sources list (#id `cite-<n>` set by
// the caller). Operates on text nodes only — never on existing markup/links — so it
// can't break code, KaTeX, or anchors.
export function markCitations(root: HTMLElement, idPrefix = ""): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const p = node.parentElement;
      if (!p) return NodeFilter.FILTER_REJECT;
      // Skip code, links, and KaTeX-rendered math.
      if (p.closest("code, pre, a, .katex")) return NodeFilter.FILTER_REJECT;
      return /\[\d+\]/.test(node.nodeValue ?? "")
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_REJECT;
    },
  });
  const targets: Text[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) targets.push(n as Text);
  for (const text of targets) {
    const frag = document.createDocumentFragment();
    const parts = (text.nodeValue ?? "").split(/(\[\d+\])/);
    for (const part of parts) {
      const m = /^\[(\d+)\]$/.exec(part);
      if (m) {
        const a = document.createElement("a");
        a.className = "cite-ref";
        a.href = `#${idPrefix}cite-${m[1]}`;
        a.textContent = part;
        a.dataset.cite = m[1];
        frag.appendChild(a);
      } else if (part) {
        frag.appendChild(document.createTextNode(part));
      }
    }
    text.replaceWith(frag);
  }
}

// Add a "Copy" button to each fenced code block. textContent only — no HTML.
function addCopyButtons(root: HTMLElement): void {
  for (const pre of Array.from(root.querySelectorAll("pre"))) {
    if (pre.parentElement?.classList.contains("code-block")) continue;
    const wrap = document.createElement("div");
    wrap.className = "code-block";
    pre.replaceWith(wrap);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "code-copy";
    btn.textContent = "Copy";
    btn.addEventListener("click", () => {
      void navigator.clipboard?.writeText(pre.textContent ?? "").then(() => {
        btn.textContent = "Copied";
        setTimeout(() => (btn.textContent = "Copy"), 1200);
      });
    });
    wrap.append(btn, pre);
  }
}
