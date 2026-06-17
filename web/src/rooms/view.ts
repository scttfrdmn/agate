// Collaborative room view (#116 PR 2) — members + the attributed message stream.
//
// Pure DOM rendering (textContent only, like panes/render.ts): a participant list (humans +
// agents, each a bounded participant) + the message log, each message showing its author and
// kind. The room's derived scope/tier are shown as the collective ceiling. No HTML sinks.

import type { RoomMessage, RoomView } from "./client";

function el(tag: string, cls = "", text?: string): HTMLElement {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

// Pure: render the participant list + scope/tier ceiling into a panel.
export function renderMembers(view: RoomView, target: HTMLElement): void {
  target.replaceChildren();
  const panel = el("section", "panel");
  panel.setAttribute("aria-label", "Room participants");
  panel.appendChild(
    el("div", "panel-title", `Room ${view.room} — ${view.scope || "(tenant-wide)"} · ${view.tier}`),
  );
  const ul = el("ul");
  ul.style.cssText = "list-style:none;display:flex;flex-wrap:wrap;gap:.5rem;margin:.4rem 0";
  for (const m of view.members) {
    const li = el("li", "status-badge");
    // a dot + "subject (kind)" — agents and humans are the same primitive, labelled by kind.
    li.appendChild(el("span", "", `${m.subject} · ${m.kind}`));
    ul.appendChild(li);
  }
  panel.appendChild(ul);
  target.appendChild(panel);
}

// Pure: render one attributed message (author + kind + text), mirroring a pane.
export function renderMessage(msg: RoomMessage): HTMLElement {
  const sec = el("section", `agate-pane ${msg.kind === "agent" ? "running" : "done"}`);
  sec.setAttribute("aria-label", `Message from ${msg.author}`);
  const head = el("header");
  head.appendChild(el("strong", "", msg.author));
  head.appendChild(el("span", "pane-status", msg.kind));
  sec.appendChild(head);
  sec.appendChild(el("div", "agate-pane-body", msg.text));
  return sec;
}

// Pure: render the full message stream into a target (the poll loop calls this on each fold).
export function renderMessages(messages: RoomMessage[], target: HTMLElement): void {
  target.replaceChildren();
  if (!messages.length) {
    target.appendChild(el("p", "cost-line", "No messages yet."));
    return;
  }
  for (const m of messages) target.appendChild(renderMessage(m));
}
