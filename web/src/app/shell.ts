// The app shell — the SPA's DOM skeleton (#221 composition layer). Pure template: given the
// mount element it writes the semantic landmarks (transcript / composer / sidebar panels) that
// main() then wires up. Depends only on UI_MODES, not on any runtime state, so it lives in the
// app/ layer alongside the other composition pieces.

import { UI_MODES } from "../router";

/** Render the SPA shell into `app`. Semantic landmarks (header/main/aside), labelled controls,
 *  and an ARIA live region so screen-reader users hear the streamed answer + run progress. */
export function renderShell(app: HTMLElement): void {
  app.innerHTML = `
    <div class="layout chat-layout">
      <main id="main" class="main-col" tabindex="-1">
        <!-- Scrolling transcript of question/answer pairs fills this region; the
             composer sits pinned at the bottom. ChatTranscript appends here. -->
        <section id="out" class="answer-region" aria-live="polite" aria-atomic="false"
                 aria-label="Conversation"></section>

        <div id="empty" class="empty-state">
          <p id="scope" class="empty-hint" role="status" aria-live="polite"></p>
        </div>

        <!-- The composer is the in-progress tail cell (Canvas #242): it flows right after the
             transcript, newest work above it, and you author at the bottom edge. There is no
             Chat/Notebook view toggle — an Ask · Code switch on the composer picks the next
             cell's kind, and reaching for Code is what grows the document a spine. -->
        <!-- Controls sit just above the composer. Model is the prominent per-turn control; Mode
             (Ask/Panel/Analyze) is demoted + separated (it competed with a redundant toggle before). -->
        <div class="composer-controls">
          <label class="control-field control-field-model">
            <span class="control-label">Model</span>
            <select id="model" aria-label="Model"
                    title="Auto routes within your entitlement + budget; or pin a model">
              <option value="auto">Auto (entitlement-aware)</option>
            </select>
          </label>
          <label class="control-field control-field-mode">
            <span class="control-label">Mode</span>
            <select id="mode" aria-label="Answer mode">
              ${UI_MODES.map((m) => `<option value="${m.value}">${m.label}</option>`).join("")}
              <optgroup label="Reasoning patterns">
                <option value="pattern:lit-review">Pattern · Literature synthesis</option>
                <option value="pattern:red-team">Pattern · Steel-man / red-team</option>
              </optgroup>
              <optgroup label="Background agent">
                <option value="agent">Agent · Capped research</option>
              </optgroup>
            </select>
          </label>
        </div>
        <form id="f" class="composer composer-bar" aria-label="Ask agate">
          <div class="input-bar">
            <!-- Ask · Code switch, Ask-weighted: a quiet mode-in, not an equal peer tab. Off = Ask
                 (a question, billed); on = Code (Python that runs locally in your browser, free). -->
            <button type="button" id="code-toggle" class="code-toggle" aria-pressed="false"
                    aria-label="Write code instead of asking"
                    title="Write code — runs locally in your browser (free)">&lt;/&gt;</button>
            <textarea id="q" rows="1" placeholder="Ask a question…"
                      autocomplete="off" aria-label="Your question"
                      aria-describedby="scope"></textarea>
            <button class="send-btn" type="submit" aria-label="Send" title="Send">&#x2191;</button>
          </div>
          <!-- Suggestion chips (sample questions; dynamic follow-ups when enabled). -->
          <div id="chips" class="suggestions" role="group" aria-label="Suggested questions"></div>
        </form>
      </main>

      <aside class="sidebar" aria-label="Session">
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Chats</div>
            <button id="new-chat" type="button" class="btn ghost btn-sm" title="Start a new chat">+ New</button>
          </div>
          <div id="chat-list" class="chat-list" aria-label="Your chats"></div>
        </div>
        <div class="panel">
          <div class="panel-title">Context</div>
          <div class="ctx-track"><div id="ctx-bar" class="ctx-fill"></div></div>
          <div id="ctx-text" class="ctx-text">0 tokens · empty</div>
          <div class="ctx-controls">
            <label class="ctx-window">
              <span>Send</span>
              <select id="ctx-window" aria-label="How much history to send">
                <option value="0">All turns</option>
                <option value="3">Last 3 turns</option>
                <option value="6">Last 6 turns</option>
                <option value="10">Last 10 turns</option>
              </select>
            </label>
            <div class="ctx-btns">
              <button id="ctx-compress" type="button" class="btn ghost btn-sm"
                title="Summarize earlier turns into a compact note (one metered call), then send that plus recent turns">Compress</button>
              <button id="ctx-clear" type="button" class="btn ghost btn-sm"
                title="Keep the transcript on screen but start the next turn from empty context">Clear</button>
            </div>
            <div id="ctx-note" class="ctx-note" hidden></div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-title">Session</div>
          <div id="scope-chips" class="scope-chips" aria-label="Your access"></div>
        </div>
        <div class="panel">
          <div class="panel-title">Running cost</div>
          <div id="cost" class="meter-total" aria-live="polite">$0.0000</div>
          <div id="cost-status" class="meter-status">this session · billed per request</div>
          <div id="budget" class="budget" hidden>
            <div class="budget-track"><div id="budget-bar" class="budget-fill"></div></div>
            <div id="budget-text" class="budget-text"></div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-title">Suggestions</div>
          <label class="toggle">
            <input id="followups-toggle" type="checkbox" />
            <span>Dynamic follow-up questions</span>
          </label>
          <p class="toggle-hint">Suggests next questions after each answer. Uses a small
            amount of extra tokens per answer.</p>
          <div id="followups-cost" class="followups-cost" hidden></div>
        </div>
      </aside>
    </div>`;
}
