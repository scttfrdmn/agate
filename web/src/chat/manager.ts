// Multi-session chat management for Ask. Each "chat" is an independent conversation:
// its own scrolling transcript DOM, its own ChatSession (multi-turn history), its own
// running token/cost tallies, and a title. Switching chats hides one transcript and
// shows another; "New chat" starts a fresh one. The session list renders into a
// sidebar element. Framework-free.

import type { ChatMessage, Transport } from "../transport";
import { ChatSession, type ContextProvider } from "./session";
import { ChatTranscript } from "./ui";
import { contextWindow } from "../router";

let nextId = 1;

// A stable, unguessable conversation id for the memory namespace. crypto.randomUUID is
// available in every target browser; the fallback keeps non-secure-context/test envs working.
function newSessionId(): string {
  const c = globalThis.crypto;
  if (c && "randomUUID" in c) return c.randomUUID();
  return `sess-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
}

export interface ChatRecord {
  id: number;
  title: string;
  // A stable conversation id for cross-session memory (#194) — the namespace key the
  // memory tool records/recalls under. Distinct from the numeric `id` (a UI handle).
  sessionId: string;
  transcript: ChatTranscript;
  el: HTMLElement; // the per-chat transcript container (shown/hidden on switch)
  history: ChatMessage[]; // accumulated turns (for rebuilding the ChatSession)
  session: ChatSession;
  modelId: string;
  // Running context estimate: tokens of conversation history sent on the NEXT turn.
  contextTokens: number;
  turns: number;
}

export interface ManagerDeps {
  // Where transcripts mount (the #out region) and what scrolls (the main column).
  appendHost: HTMLElement;
  scrollHost: HTMLElement;
  listHost: HTMLElement; // sidebar session list
  transport: Transport;
  contextProvider?: ContextProvider;
  // Called when the active chat changes or its context usage updates, so the
  // surrounding UI (context gauge, empty-state) can refresh.
  onActiveChange?: (chat: ChatRecord) => void;
}

// Conservative char/4 token estimate (matches the server's own estimator spirit).
function estimateTokens(messages: ChatMessage[]): number {
  const chars = messages.reduce((n, m) => n + m.content.length, 0);
  return Math.ceil(chars / 4);
}

export class ChatManager {
  private chats: ChatRecord[] = [];
  private active!: ChatRecord;

  constructor(private readonly deps: ManagerDeps) {
    this.newChat();
  }

  get current(): ChatRecord {
    return this.active;
  }

  /** The context window of the active chat's model, for the usage gauge. */
  get contextWindow(): number {
    return contextWindow(this.active.modelId);
  }

  /** Start a fresh chat and switch to it. */
  newChat(modelId = "openai.gpt-oss-20b-1:0"): ChatRecord {
    const el = document.createElement("div");
    el.className = "chat-pane";
    this.deps.appendHost.appendChild(el);
    const transcript = new ChatTranscript(el, this.deps.scrollHost);
    const history: ChatMessage[] = [];
    const chat: ChatRecord = {
      id: nextId++,
      title: "New chat",
      sessionId: newSessionId(),
      transcript,
      el,
      history,
      modelId,
      session: new ChatSession(this.deps.transport, modelId, undefined, undefined,
        this.deps.contextProvider, history),
      contextTokens: 0,
      turns: 0,
    };
    this.chats.push(chat);
    this.switchTo(chat.id);
    this.renderList();
    return chat;
  }

  switchTo(id: number): void {
    const chat = this.chats.find((c) => c.id === id);
    if (!chat) return;
    this.active = chat;
    for (const c of this.chats) c.el.hidden = c.id !== id;
    this.renderList();
    this.deps.onActiveChange?.(chat);
  }

  /** The active chat's ChatSession, rebuilt against `modelId` if it changed (so a
   *  model switch keeps the conversation history). */
  sessionFor(modelId: string): ChatSession {
    const chat = this.active;
    if (chat.modelId !== modelId) {
      chat.modelId = modelId;
      chat.session = new ChatSession(this.deps.transport, modelId, undefined, undefined,
        this.deps.contextProvider, chat.history);
    }
    return chat.session;
  }

  /** Record a completed turn into the active chat: history + context estimate + title. */
  recordTurn(question: string, _answer: string): void {
    const chat = this.active;
    // ChatSession already pushed the user+assistant messages into `history` (shared
    // array reference), so just recompute the derived figures.
    chat.turns += 1;
    chat.contextTokens = estimateTokens(chat.history);
    if (chat.turns === 1) {
      chat.title = question.length > 40 ? question.slice(0, 40).trimEnd() + "…" : question;
    }
    this.renderList();
    this.deps.onActiveChange?.(chat);
  }

  private renderList(): void {
    const host = this.deps.listHost;
    host.replaceChildren(
      ...this.chats.map((c) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chat-list-item" + (c.id === this.active?.id ? " active" : "");
        btn.textContent = c.title;
        btn.title = c.title;
        btn.addEventListener("click", () => this.switchTo(c.id));
        return btn;
      }),
    );
  }
}
