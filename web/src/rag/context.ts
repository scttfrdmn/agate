// Pure RAG context assembly (design §4). No SDK calls — unit-testable.
//
// Takes the chunks retrieved from the tenant's S3 Vectors index and folds them
// into the chat request as a system message, so the model answers grounded in the
// user's own in-scope documents. Retrieval scope IS the access boundary: these
// chunks only ever come from the index the scoped credentials could read.

import type { ChatMessage } from "../transport";

export interface RetrievedChunk {
  key: string;
  text: string;
  sourceKey?: string;
  // Connector provenance (#133): for a chunk ingested via a connector, the source
  // SYSTEM + item it came from, shown in the answer's Sources footer.
  sourceSystem?: string;
  sourceItem?: string;
  distance?: number;
}

// Build a grounding system prompt from retrieved chunks. Returns null when there
// is nothing to inject (so the caller sends the plain question unchanged).
export function buildContextMessage(chunks: RetrievedChunk[]): ChatMessage | null {
  const usable = chunks.filter((c) => c.text && c.text.trim().length > 0);
  if (usable.length === 0) return null;

  const sources = usable
    .map((c, i) => {
      const cite = c.sourceKey ? ` (source: ${c.sourceKey})` : "";
      return `[${i + 1}]${cite}\n${c.text.trim()}`;
    })
    .join("\n\n");

  return {
    role: "system",
    content:
      "You are a grounded study assistant for the user's own in-scope course documents, shown " +
      "below. Ground every FACTUAL claim in these excerpts and cite them by their [n] marker; " +
      "for facts, do not rely on outside knowledge. If a factual answer isn't in the context, do " +
      "NOT guess: say the question is outside the documents available to this session (e.g. \"I " +
      "couldn't find that in the documents available to you — the retrieved material covers " +
      "<briefly name the topics present>. Try rephrasing, or ask about that material.\") and name " +
      "the topics the excerpts actually cover.\n\n" +
      "APPLYING the material is encouraged, not refused: if the user asks you to compute, plot, " +
      "derive, work an example, or write Python using concepts, equations, or data that ARE in " +
      "the context, do it — cite the excerpt(s) the formula or value came from. When code helps " +
      "(a plot, a calculation, a simulation), write a runnable ```python code block; the user can " +
      "run it locally. Only the underlying facts must come from the documents; the computation " +
      "that applies them does not need its own citation.\n\n" +
      `Context:\n${sources}`,
  };
}

// Prepend the grounding context (if any) to the conversation for one turn.
export function withContext(
  messages: ChatMessage[],
  chunks: RetrievedChunk[],
): ChatMessage[] {
  const ctx = buildContextMessage(chunks);
  return ctx ? [ctx, ...messages] : messages;
}
