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

import type { BudgetStatus, Transport } from "../transport";

export interface FollowupResult {
  questions: string[];
  // The metered cost of generating these suggestions, so the UI can report it (the
  // toggle warned this costs extra). Undefined when the transport doesn't meter.
  cost?: number;
  usage?: { inputTokens: number; outputTokens: number };
  budget?: BudgetStatus;
}

// Suggestion chips are rendered as PLAIN TEXT (and the clicked text is submitted
// verbatim as the next prompt) — they never go through the Markdown/KaTeX renderer.
// So strip LaTeX math delimiters here, leaving the inner expression as readable text;
// otherwise a suggestion like "value of \(W\)?" shows (and submits) the raw "\(W\)".
export function stripMathDelimiters(s: string): string {
  return s
    .replace(/\$\$([\s\S]+?)\$\$/g, (_m, tex) => tex.trim()) // $$…$$
    .replace(/\\\[([\s\S]+?)\\\]/g, (_m, tex) => tex.trim()) // \[…\]
    .replace(/\\\(([\s\S]+?)\\\)/g, (_m, tex) => tex.trim()) // \(…\)
    .replace(/\$(?!\s)([^\n$]+?)(?<!\s)\$/g, (_m, tex) => tex.trim()) // $…$
    .replace(/\s{2,}/g, " ")
    .trim();
}

// Pure: parse the model's reply into at most `max` trimmed, de-duplicated questions.
// Accepts a newline or numbered list; strips bullets/numbering and surrounding quotes.
export function parseFollowups(text: string, max = 3): string[] {
  const out: string[] = [];
  for (const raw of text.split("\n")) {
    const line = stripMathDelimiters(
      raw
        .replace(/^\s*(?:[-*•]|\d+[.)])\s*/, "") // bullet / "1." / "1)"
        .replace(/^["'“”]+|["'“”]+$/g, "")
        .trim(),
    );
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

// When corpus context is supplied, the suggestions must stay ANSWERABLE from that
// context — otherwise we cheerfully suggest questions the grounded assistant will then
// refuse ("I couldn't find that in the documents"). This prompt hard-constrains the
// model to the provided excerpts.
const GROUNDED_PROMPT =
  "You are suggesting follow-up questions for a retrieval-grounded assistant that can " +
  "ONLY answer from the CONTEXT excerpts below. Suggest 3 short follow-up questions that " +
  "are clearly answerable FROM THE CONTEXT — do not suggest anything the context can't " +
  "support. Output ONLY the questions, one per line, no numbering, no preamble. Each must " +
  "be a single concise question ending in '?'. If the context can't support 3 good " +
  "questions, output fewer.";

/** Generate follow-up questions for a finished Q&A. When `context` is given (the
 *  retrieved corpus excerpts that grounded the answer), suggestions are constrained to
 *  be answerable from it, so we don't propose questions the assistant will refuse.
 *  Returns empty questions (and no cost) on any failure. */
export async function suggestFollowups(
  transport: Transport,
  modelId: string,
  question: string,
  answer: string,
  context?: string,
): Promise<FollowupResult> {
  try {
    let text = "";
    let cost: number | undefined;
    let usage: FollowupResult["usage"];
    let budget: BudgetStatus | undefined;
    // 512, not a tiny budget: gpt-oss-class reasoning models spend output tokens on
    // internal reasoning before the answer, so a small cap (e.g. 128) gets consumed
    // by reasoning and emits NO answer text — the suggestions came back empty and the
    // UI fell back to the stock chips. 512 leaves headroom for the actual questions.
    // The answer itself is trimmed to keep the prompt (and so the cost) modest.
    const grounded = context && context.trim().length > 0;
    const instruction = grounded ? GROUNDED_PROMPT : PROMPT;
    const contextBlock = grounded ? `CONTEXT:\n${context!.slice(0, 4000)}\n\n` : "";
    for await (const chunk of transport.converse({
      modelId,
      maxTokens: 512,
      messages: [
        {
          role: "user",
          content: `${contextBlock}Question: ${question}\n\nAnswer: ${answer.slice(0, 1500)}\n\n${instruction}`,
        },
      ],
    })) {
      if (chunk.delta) text += chunk.delta;
      if (chunk.usage) usage = chunk.usage;
      if (chunk.cost !== undefined) cost = chunk.cost;
      if (chunk.budget) budget = chunk.budget;
    }
    return { questions: parseFollowups(text), cost, usage, budget };
  } catch {
    return { questions: [] };
  }
}
