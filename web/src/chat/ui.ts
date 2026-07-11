// Chat transcript UI for Ask — a scrolling history of question/answer pairs that
// behaves like a chatbot (user bubble right, assistant answer left, thinking dots,
// smooth auto-scroll), mirroring the aws-agentcore-demo. Pure DOM construction +
// small controller; no framework. The markdown/math/citation rendering is delegated
// to render/markdown.ts (the XSS boundary).

import type { RetrievedChunk } from "../rag/context";
import type { BudgetStatus } from "../transport";
import { renderInto } from "../render/markdown";
import { modelLabel } from "../router";

export interface AnswerMeta {
  usage?: { inputTokens: number; outputTokens: number };
  cost?: number;
  budget?: BudgetStatus;
  modelId?: string;
  // Why the server routed to this model (shown as a tooltip on the model tag when
  // "auto" was used). Undefined for a pinned model.
  modelReason?: string;
}

// One assistant turn's live handle: the caller streams text in, then finalizes.
export interface AssistantTurn {
  /** Append streamed plain text (shown live, pre-wrap). */
  appendDelta(delta: string): void;
  /** Show the animated thinking indicator (before the first delta). */
  thinking(): void;
  /** Replace the streamed text with rendered Markdown + math + the sources/receipt. */
  finalize(text: string, sources: RetrievedChunk[], meta: AnswerMeta): void;
  /** Render an error in place of the answer. */
  fail(message: string): void;
}

export class ChatTranscript {
  private readonly history: HTMLElement;

  // `appendHost` is where the transcript DOM lives; `scrollHost` is the element that
  // actually scrolls (the main column, since the composer flows beneath the
  // transcript). They differ so new answers scroll the column, not just the region.
  constructor(
    appendHost: HTMLElement,
    private readonly scrollHost: HTMLElement = appendHost,
  ) {
    this.history = document.createElement("div");
    this.history.className = "chat-history";
    appendHost.appendChild(this.history);
    this.watchScroll();
  }

  private userScrolled = false;

  /** Start a new turn: append the user bubble + an empty assistant bubble. */
  begin(question: string): AssistantTurn {
    const pair = el("div", "msg-pair");

    const userBubble = el("div", "user-bubble");
    const qBadge = el("div", "bubble-badge");
    qBadge.textContent = "You asked";
    const qText = el("div", "q-text");
    qText.textContent = question; // verbatim, never markdown
    userBubble.append(qBadge, qText);

    // The assistant reply lives in its OWN bubble (mirroring the question bubble):
    // a header row ("Answer" + the model that replied, filled in on finalize) and
    // the answer body. While waiting, a "Thinking …" indicator sits in the body.
    const asst = el("div", "assistant-bubble");
    const head = el("div", "bubble-head");
    const aBadge = el("div", "bubble-badge");
    aBadge.textContent = "Answer";
    const modelTag = el("div", "model-tag");
    modelTag.hidden = true;
    head.append(aBadge, modelTag);
    const body = el("div", "answer-body");
    const thinking = el("div", "thinking");
    const label = el("span", "thinking-label");
    label.textContent = "Thinking";
    const dotsEl = el("span", "thinking-dot");
    dotsEl.innerHTML = "<span></span><span></span><span></span>";
    thinking.append(label, dotsEl);
    asst.append(head, thinking, body);

    pair.append(userBubble, asst);
    this.history.appendChild(pair);
    this.userScrolled = false; // a new question resumes auto-scroll
    this.scrollDown();

    let acc = "";
    let think: HTMLElement | null = thinking;
    const clearThinking = () => {
      if (think) {
        think.remove();
        think = null;
      }
    };

    return {
      thinking: () => {
        /* the indicator is already shown */
      },
      appendDelta: (delta) => {
        clearThinking();
        acc += delta;
        body.textContent = acc; // live plain-text stream
        this.scrollDown();
      },
      finalize: (text, sources, meta) => {
        clearThinking();
        if (meta.modelId) {
          modelTag.textContent = modelLabel(meta.modelId);
          // When the server routed (auto), show the rationale on hover + an "auto" hint.
          if (meta.modelReason) {
            modelTag.title = `Auto-routed: ${meta.modelReason}`;
            modelTag.classList.add("routed");
          }
          modelTag.hidden = false;
        }
        if (text.trim()) {
          renderInto(body, text);
          body.classList.add("rendered");
        } else {
          body.textContent = acc;
        }
        if (sources.length) asst.appendChild(renderSources(sources));
        asst.appendChild(renderReceipt(meta));
        asst.appendChild(copyAnswerBtn(text));
        this.scrollDown();
      },
      fail: (message) => {
        clearThinking();
        const err = el("div", "error-msg");
        err.setAttribute("role", "alert");
        err.textContent = `Error: ${message}`;
        body.replaceWith(err);
        this.scrollDown();
      },
    };
  }

  /** Smoothly pin to the bottom unless the user scrolled up to read. */
  private scrollDown(): void {
    if (this.userScrolled) return;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (!this.userScrolled) {
          this.scrollHost.scrollTo({ top: this.scrollHost.scrollHeight, behavior: "smooth" });
        }
      }),
    );
  }

  private watchScroll(): void {
    let last = 0;
    this.scrollHost.addEventListener(
      "scroll",
      () => {
        const host = this.scrollHost;
        const fromBottom = host.scrollHeight - host.scrollTop - host.clientHeight;
        if (host.scrollTop < last && fromBottom > 120) this.userScrolled = true;
        if (fromBottom < 24) this.userScrolled = false;
        last = host.scrollTop;
      },
      { passive: true },
    );
  }
}

// --- pieces ----------------------------------------------------------------

// Exported so the notebook view (web/src/chat/notebook-ui.ts) reuses the exact Sources /
// receipt / copy markup. `idPrefix` namespaces the citation anchor ids so multiple cells
// on one page don't collide (a chat transcript renders one answer at a time → default "").
export function renderSources(chunks: RetrievedChunk[], idPrefix = ""): HTMLElement {
  const box = el("div", "sources");
  const title = el("div", "sources-title");
  title.textContent = `Sources (${chunks.length})`;
  box.appendChild(title);
  const list = el("ol", "sources-list");
  chunks.forEach((c, i) => {
    const li = el("li", "source-item");
    li.id = `${idPrefix}cite-${i + 1}`; // citation anchors ([n]) scroll here
    li.append(sourceLabelNode(c));
    const snippet = el("span", "source-snippet");
    snippet.textContent = c.text.length > 160 ? c.text.slice(0, 160).trimEnd() + "…" : c.text;
    li.append(snippet);
    list.appendChild(li);
  });
  box.appendChild(list);
  return box;
}

// A web source (sourceSystem === "web", sourceItem = the fetched https URL) renders as a
// clickable external link; everything else is plain text (corpus docs aren't web-served).
function sourceLabelNode(c: RetrievedChunk): HTMLElement {
  if (c.sourceSystem === "web" && c.sourceItem && /^https:\/\//.test(c.sourceItem)) {
    const a = document.createElement("a");
    a.className = "source-label source-link";
    a.href = c.sourceItem;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = c.sourceItem;
    return a;
  }
  const label = el("span", "source-label");
  label.textContent = sourceLabel(c);
  return label;
}

function sourceLabel(c: RetrievedChunk): string {
  if (c.sourceSystem && c.sourceItem) return `${c.sourceSystem}: ${c.sourceItem}`;
  if (c.sourceKey) return c.sourceKey;
  return c.key || "document";
}

export function renderReceipt(meta: AnswerMeta): HTMLElement {
  const box = el("div", "msg-receipt");
  const row = (label: string, value: string) => {
    const r = el("div", "msg-receipt-row");
    const l = el("span", "mr-label");
    l.textContent = label;
    const v = el("span", "mr-cost");
    v.textContent = value;
    r.append(l, v);
    return r;
  };
  if (meta.usage) {
    box.appendChild(
      row(
        "Tokens",
        `${meta.usage.inputTokens.toLocaleString()} in / ${meta.usage.outputTokens.toLocaleString()} out`,
      ),
    );
  }
  if (typeof meta.cost === "number") {
    const total = el("div", "msg-receipt-total");
    const l = el("span", "");
    l.textContent = "This question";
    const v = el("span", "");
    v.textContent = `$${meta.cost.toFixed(6)}`;
    total.append(l, v);
    box.appendChild(total);
  }
  return box;
}

export function copyAnswerBtn(text: string): HTMLElement {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "answer-copy";
  btn.textContent = "Copy answer";
  btn.addEventListener("click", () => {
    void navigator.clipboard?.writeText(text).then(() => {
      btn.textContent = "Copied";
      setTimeout(() => (btn.textContent = "Copy answer"), 1200);
    });
  });
  return btn;
}

function el(tag: string, cls: string): HTMLElement {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
}
