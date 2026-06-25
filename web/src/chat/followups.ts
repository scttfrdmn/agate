// Dynamic follow-up suggestions (opt-in — costs a small amount of extra tokens per
// answer). After an answer, ask the model for a few short follow-up questions the
// user is likely to want next, grounded in the just-finished exchange. Returned as
// plain strings the UI renders as suggestion chips.
//
// This reuses the SAME transport (and therefore the same gated/metered choke point)
// as a normal turn, so the suggestion call is itself budget-governed — it can't
// escape the user's entitlement. Kept deliberately small (low max_tokens, one turn,
// no history) so it's cheap. Failures are swallowed: suggestions are a nicety, never
// allowed to disrupt the answer.

import type { Transport } from "../transport";

// Pure: parse the model's reply into at most `max` trimmed, de-duplicated questions.
// Accepts a newline or numbered list; strips bullets/numbering and surrounding quotes.
export function parseFollowups(text: string, max = 3): string[] {
  const out: string[] = [];
  for (const raw of text.split("\n")) {
    const line = raw
      .replace(/^\s*(?:[-*•]|\d+[.)])\s*/, "") // bullet / "1." / "1)"
      .replace(/^["'“”]+|["'“”]+$/g, "")
      .trim();
    // Must look like a question: contains a '?' and at least one letter.
    if (!line.includes("?") || !/[a-z]/i.test(line)) continue;
    if (!out.some((q) => q.toLowerCase() === line.toLowerCase())) out.push(line);
    if (out.length >= max) break;
  }
  return out;
}

const PROMPT =
  "Based on the question and answer above, suggest 3 short follow-up questions the " +
  "reader is most likely to ask next. Output ONLY the questions, one per line, no " +
  "numbering, no preamble. Each must be a single concise question ending in '?'.";

/** Generate follow-up questions for a finished Q&A. Returns [] on any failure. */
export async function suggestFollowups(
  transport: Transport,
  modelId: string,
  question: string,
  answer: string,
): Promise<string[]> {
  try {
    let text = "";
    // 512, not a tiny budget: gpt-oss-class reasoning models spend output tokens on
    // internal reasoning before the answer, so a small cap (e.g. 128) gets consumed
    // by reasoning and emits NO answer text — the suggestions came back empty and the
    // UI fell back to the stock chips. 512 leaves headroom for the actual questions.
    // The answer itself is trimmed to keep the prompt (and so the cost) modest.
    for await (const chunk of transport.converse({
      modelId,
      maxTokens: 512,
      messages: [
        {
          role: "user",
          content: `Question: ${question}\n\nAnswer: ${answer.slice(0, 1500)}\n\n${PROMPT}`,
        },
      ],
    })) {
      if (chunk.delta) text += chunk.delta;
    }
    return parseFollowups(text);
  } catch {
    return [];
  }
}
