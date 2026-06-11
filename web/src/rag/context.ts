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
      "Answer using only the context below. If the context does not contain " +
      "the answer, say so rather than guessing. Cite sources by their [n] marker.\n\n" +
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
