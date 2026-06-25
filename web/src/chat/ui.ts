// Chat transcript UI for Ask — a scrolling history of question/answer pairs that
// behaves like a chatbot (user bubble right, assistant answer left, thinking dots,
// smooth auto-scroll), mirroring the aws-agentcore-demo. Pure DOM construction +
// small controller; no framework. The markdown/math/citation rendering is delegated
// to render/markdown.ts (the XSS boundary).

import type { RetrievedChunk } from "../rag/context";
import type { BudgetStatus } from "../transport";
import { renderInto } from "../render/markdown";

export interface AnswerMeta {
  usage?: { inputTokens: number; outputTokens: number };
  cost?: number;
  budget?: BudgetStatus;
  modelId?: string;
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

  constructor(private readonly scrollHost: HTMLElement) {
    this.history = document.createElement("div");
    this.history.className = "chat-history";
    scrollHost.appendChild(this.history);
    this.watchScroll();
  }

  private userScrolled = false;

  /** Start a new turn: append the user bubble + an empty assistant bubble. */
  begin(question: string): AssistantTurn {
    const pair = el("div", "msg-pair");

    const userBubble = el("div", "user-bubble");
    const qBadge = el("div", "q-badge");
    qBadge.textContent = "You asked";
    const qText = el("div", "q-text");
    qText.textContent = question; // verbatim, never markdown
    userBubble.append(qBadge, qText);

    const asst = el("div", "assistant-bubble");
    const body = el("div", "answer-body");
    const thinkingDots = el("div", "thinking-dot");
    thinkingDots.innerHTML = "<span></span><span></span><span></span>";
    asst.append(thinkingDots, body);

    pair.append(userBubble, asst);
    this.history.appendChild(pair);
    this.userScrolled = false; // a new question resumes auto-scroll
    this.scrollDown();

    let acc = "";
    let dots: HTMLElement | null = thinkingDots;
    const clearDots = () => {
      if (dots) {
        dots.remove();
        dots = null;
      }
    };

    return {
      thinking: () => {
        /* dots are already shown */
      },
      appendDelta: (delta) => {
        clearDots();
        acc += delta;
        body.textContent = acc; // live plain-text stream
        this.scrollDown();
      },
      finalize: (text, sources, meta) => {
        clearDots();
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
        clearDots();
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

function renderSources(chunks: RetrievedChunk[]): HTMLElement {
  const box = el("div", "sources");
  const title = el("div", "sources-title");
  title.textContent = `Sources (${chunks.length})`;
  box.appendChild(title);
  const list = el("ol", "sources-list");
  chunks.forEach((c, i) => {
    const li = el("li", "source-item");
    li.id = `cite-${i + 1}`; // citation anchors ([n]) scroll here
    const label = el("span", "source-label");
    label.textContent = sourceLabel(c);
    const snippet = el("span", "source-snippet");
    snippet.textContent = c.text.length > 160 ? c.text.slice(0, 160).trimEnd() + "…" : c.text;
    li.append(label, snippet);
    list.appendChild(li);
  });
  box.appendChild(list);
  return box;
}

function sourceLabel(c: RetrievedChunk): string {
  if (c.sourceSystem && c.sourceItem) return `${c.sourceSystem}: ${c.sourceItem}`;
  if (c.sourceKey) return c.sourceKey;
  return c.key || "document";
}

function renderReceipt(meta: AnswerMeta): HTMLElement {
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

function copyAnswerBtn(text: string): HTMLElement {
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
