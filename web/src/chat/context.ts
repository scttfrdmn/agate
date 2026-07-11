// Context selection — decide which of a chat's messages are actually SENT to the model
// on the next turn, separate from the full transcript the user sees. Pure and unit-tested;
// the transcript (and cross-session memory) keep the complete history, while this narrows
// what goes up the wire, which is what the CONTEXT gauge and the token bill reflect.
//
// Three controls compose into one policy:
//   • floor      — clear-context: drop every turn before this body index from the send
//                  (the transcript stays on screen; the model just starts fresh here).
//   • maxTurns   — sliding window: send only the last N turns (a turn = user+assistant).
//   • summary    — summarize-and-compress: a compact note standing in for older turns,
//                  sent as a system message ahead of the retained turns.
//
// Leading system messages (persona / RAG grounding seed) are always kept — they aren't
// conversational turns and dropping them would change behaviour, not just context size.

import type { ChatMessage } from "../transport";

export interface ContextPolicy {
  // Body index (into non-system messages) before which turns are NOT sent. Default 0.
  floor?: number;
  // Keep only the last N turns (user+assistant pairs) of the retained body. Undefined =
  // no window (send everything from the floor on).
  maxTurns?: number;
  // A summary of the turns hidden by the floor, sent as a system message. Undefined = none.
  summary?: string;
}

/**
 * Project the full history into the messages to send under `policy`. Pure. Order:
 * [leading system messages] → [summary as a system message, if any] → [retained turns].
 * The retained turns are the body from `floor` on, then windowed to the last `maxTurns`
 * pairs, trimmed to start on a user message so the user/assistant alternation stays valid.
 */
export function selectContext(history: ChatMessage[], policy: ContextPolicy = {}): ChatMessage[] {
  const { floor = 0, maxTurns, summary } = policy;
  const head = history.filter((m) => m.role === "system");
  const body = history.filter((m) => m.role !== "system");

  let kept = floor > 0 ? body.slice(floor) : body;
  if (typeof maxTurns === "number" && maxTurns >= 0) {
    const maxMsgs = maxTurns * 2; // a turn is a user+assistant pair
    if (kept.length > maxMsgs) kept = kept.slice(kept.length - maxMsgs);
    // A window can start mid-pair (on an assistant); drop leading non-user messages so
    // the sent sequence begins with a user turn.
    while (kept.length && kept[0].role !== "user") kept = kept.slice(1);
  }

  const out: ChatMessage[] = [...head];
  if (summary && summary.trim()) {
    out.push({ role: "system", content: `Summary of earlier conversation:\n${summary.trim()}` });
  }
  out.push(...kept);
  return out;
}

/** Count of non-system messages (turns' worth) in a history — the body length, used as
 *  the clear-context floor ("drop everything up to now"). Pure. */
export function bodyLength(history: ChatMessage[]): number {
  return history.reduce((n, m) => (m.role === "system" ? n : n + 1), 0);
}
