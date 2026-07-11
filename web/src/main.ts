// SPA entry — the academic interaction model UI (§10.2, demo-readiness #39).
//
// Three modes share one client surface:
//   Ask     -> Tier 0 browser-direct ConverseStream (BedrockTransport), streamed
//              into the answer pane.
//   Panel   -> AgentCore Runtime: N models read the same evidence; panes + the
//              side-by-side divergence view render from the run event stream.
//   Analyze -> AgentCore Runtime + Code Interpreter: editable code cell + chart.
//
// The mode is the user's explicit choice (academics prefer control); the router
// only suggests a default for free-form input. Panel/Analyze go through the agent
// path, which derives the caller's tier/tenant from the IdP token server-side
// (SEC-4b) — the SPA just forwards the token, never a tier.

import "@fontsource/atkinson-hyperlegible/400.css";
import "@fontsource/atkinson-hyperlegible/700.css";
import "katex/dist/katex.min.css";
import "./styles/agate.css";

import { fetchAdmin, renderAdmin } from "./admin/view";
import { CredentialManager } from "./auth/credentials";
import { currentToken, isLoggedIn, login, logout, type LoginConfig } from "./auth/login";
import { mountChrome } from "./chrome/nav";
import { config } from "./config";
import { type AuthoringOptions, AuthoringClient, type TemplateRow } from "./drafting/builder";
import { buildSpecFromForm } from "./drafting/builder";
import { DeployClient, DraftClient, renderDraft } from "./drafting/draft";
import { RoomClient } from "./rooms/client";
import { renderMembers, renderMessages } from "./rooms/view";
import { CorpusClient } from "./corpus/client";
import { renderCorpus } from "./corpus/view";
import { MemoryClient } from "./memory/client";
import { reduce, type RunState, emptyRunState } from "./events/collector";
import type { RunEvent } from "./events/protocol";
import { renderCells, renderPanel } from "./panes/render";
import { ChatManager } from "./chat/manager";
import { SessionMeter } from "./chat/meter";
import { suggestFollowups } from "./chat/followups";
import { type NotebookCell, newCell } from "./chat/notebook";
import { renderNotebook } from "./chat/notebook-ui";
import { runCell } from "./chat/notebook-run";
import { type RetrievedChunk, withContext } from "./rag/context";
import { Retriever } from "./rag/retriever";
import { AgentCoreTransport } from "./transport/agentcore";
import { BedrockTransport } from "./transport/bedrock";
import { OpenAITransport } from "./transport/openai";
import type { Transport } from "./transport";
import {
  AUTO,
  type Tier,
  type UiMode,
  UI_MODES,
  contextWindow as contextWindowFor,
  modelOptions,
  uiToRoute,
} from "./router";

// IdP token provider. With the demo Hosted UI wired (VITE_COGNITO_*), this is the
// id_token captured from the login redirect (stored in sessionStorage, scrubbed
// from the URL). Without it, it falls back to a manual `#idp_token=<jwt>` in the
// hash. Either way the broker + agent verify it server-side (RS256/JWKS).
function idpToken(): string {
  return currentToken();
}

// Hosted-UI config, present only when the demo IdP env vars are set.
const loginConfig: LoginConfig | null = config.cognitoDomain
  ? {
      domain: config.cognitoDomain,
      clientId: config.cognitoClientId,
      // The site ROOT, not origin+pathname: Cognito requires the redirect_uri to
      // match a registered callback EXACTLY, and we register `<origin>/`. Using the
      // live pathname would mismatch on any deep link / leftover path ("An error was
      // encountered with the requested page"). The SPA serves the same app at root.
      redirectUri: location.origin + "/",
    }
  : null;

function render(app: HTMLElement): void {
  // Semantic landmarks (header / main / aside) + labelled controls + an ARIA
  // live region so screen-reader users hear the streamed answer and run progress.
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

        <!-- Composer + chips flow right after the transcript: empty, they sit near
             the top; as answers stream in the transcript grows and pushes them down
             (the whole column scrolls). -->
        <form id="f" class="composer composer-bar" aria-label="Ask agate">
          <div class="composer-controls">
            <!-- Chat | Notebook view toggle (#185): a view of the current chat. -->
            <div id="view-toggle" class="view-toggle" role="group" aria-label="View">
              <button type="button" class="view-btn active" data-view="chat" aria-pressed="true">Chat</button>
              <button type="button" class="view-btn" data-view="notebook" aria-pressed="false">Notebook</button>
            </div>
            <select id="mode" aria-label="Mode">
              ${UI_MODES.map((m) => `<option value="${m.value}">${m.label}</option>`).join("")}
              <optgroup label="Reasoning patterns">
                <option value="pattern:lit-review">Pattern · Literature synthesis</option>
                <option value="pattern:red-team">Pattern · Steel-man / red-team</option>
              </optgroup>
            </select>
            <select id="model" aria-label="Model"
                    title="Auto routes within your entitlement + budget; or pin a model">
              <option value="auto">Auto (entitlement-aware)</option>
            </select>
          </div>
          <div class="input-bar">
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

// Render the session's verified access as chips (tier / tenant / affiliation /
// courses). These are display-only echoes of the broker's scope — authority lives
// server-side. Called once creds.scope is known.
function renderScopeChips(scope: {
  tier?: string;
  tenant?: string;
  affiliation?: string;
  courses?: string[];
}): void {
  const host = document.getElementById("scope-chips");
  if (!host) return;
  const chips: Array<[string, string]> = [];
  if (scope.tier) chips.push(["tier", scope.tier]);
  if (scope.tenant) chips.push(["tenant", scope.tenant]);
  if (scope.affiliation) chips.push(["role", scope.affiliation]);
  for (const c of scope.courses ?? []) chips.push(["course", c]);
  host.replaceChildren(
    ...chips.map(([k, v]) => {
      const chip = document.createElement("span");
      chip.className = "scope-chip";
      const key = document.createElement("span");
      key.className = "scope-chip-key";
      key.textContent = k;
      const val = document.createElement("span");
      val.className = "scope-chip-val";
      val.textContent = v;
      chip.append(key, val);
      return chip;
    }),
  );
}

// Show the recalled "what I remember about you" block in the empty-chat state (#194
// follow-up), so a returning user sees their continuity before asking. Replaces any prior
// seed; cleared when a turn arrives (the empty state hides).
function renderMemorySeed(text: string): void {
  const empty = document.getElementById("empty");
  if (!empty || empty.hidden) return;
  let seed = document.getElementById("memory-seed");
  if (!seed) {
    seed = document.createElement("div");
    seed.id = "memory-seed";
    seed.className = "memory-seed";
    empty.appendChild(seed);
  }
  const title = document.createElement("div");
  title.className = "memory-seed-title";
  title.textContent = "From your earlier sessions";
  const body = document.createElement("div");
  body.className = "memory-seed-body";
  body.textContent = text.replace(/^Relevant remembered context:\n/, "");
  seed.replaceChildren(title, body);
}

// Errors are announced assertively (role=alert) so a screen reader interrupts to
// read them, rather than waiting for the polite answer queue.
function renderError(out: HTMLElement, message: string): void {
  const box = document.createElement("p");
  box.className = "error-msg";
  box.setAttribute("role", "alert");
  box.textContent = `Error: ${message}`;
  out.replaceChildren(box);
}

function main(): void {
  const app = document.getElementById("app");
  if (!app) return;
  render(app);

  // The auth (login/logout) control lives in the shared top bar.
  const authBtn = document.createElement("button");
  authBtn.type = "button";
  authBtn.className = "btn ghost";

  // Top bar + pop-out navigation. The Admin item is offered whenever the console
  // API is configured; the API itself is the gate (a non-admin session gets a 403,
  // surfaced as "not authorized"). So we never need to trust a client-side role.
  const navItems = [
    { label: "Ask", icon: "💬", href: "#", current: true, onSelect: () => selectMode("ask") },
    { label: "Panel", icon: "▤", href: "#", onSelect: () => selectMode("panel") },
    { label: "Analyze", icon: "📊", href: "#", onSelect: () => selectMode("analyze") },
  ];
  if (config.adminUrl) {
    navItems.push({ label: "Admin · Usage", icon: "🛠", href: "#", onSelect: () => showAdmin() });
  }
  // Natural-language drafting (#118c). The endpoint clamps any draft to the author's
  // verified authority server-side; this screen just describes → renders the bounded plan.
  if (config.draftingUrl) {
    navItems.push({ label: "Draft an agent", icon: "✎", href: "#", onSelect: () => showDraft() });
  }
  // Graphical authoring (#117). The bounded menu is pre-clamped to the author's reach
  // server-side; the assembled spec funnels through the same compiler clamp as a draft.
  if (config.authoringUrl) {
    navItems.push({ label: "Build an agent", icon: "🧩", href: "#", onSelect: () => showBuild() });
  }
  // Collaborative rooms (#116). The room's reach is the server-enforced intersection of its
  // members; every message is attributed + budget-gated. Polling transport ($0-idle).
  if (config.roomsUrl) {
    navItems.push({ label: "Rooms", icon: "👥", href: "#", onSelect: () => showRoom() });
  }
  // Corpus (#191). Upload + browse the user's own in-scope documents; the endpoint
  // fences every read/write to the verified tenant/scope. Gated on VITE_CORPUS_URL.
  if (config.corpusUrl) {
    navItems.push({ label: "Documents", icon: "📄", href: "#", onSelect: () => showCorpus() });
  }
  const { topbar } = mountChrome({
    brand: "agate",
    tag: "GenAI gateway",
    actions: [authBtn],
    items: navItems,
  });
  app.insertBefore(topbar, app.firstChild);

  function selectMode(value: string): void {
    roomPollToken += 1; // leaving the room view stops its poll loop
    const sel = document.getElementById("mode") as HTMLSelectElement | null;
    if (sel) sel.value = value;
    (document.getElementById("q") as HTMLInputElement | null)?.focus();
  }

  // Render the governed-access console into the main region. Admin-gated server-side.
  async function showAdmin(): Promise<void> {
    const out = document.getElementById("out");
    if (!out) return;
    roomPollToken += 1; // leaving the room view stops its poll loop
    out.replaceChildren();
    out.setAttribute("aria-busy", "true");
    try {
      const payload = await fetchAdmin(config.adminUrl, idpToken());
      renderAdmin(payload, out);
    } catch (err) {
      renderError(out, (err as Error).message);
    } finally {
      out.setAttribute("aria-busy", "false");
    }
  }

  // The drafting client is created after login (it needs scoped creds for SigV4). The
  // Draft screen is offered in the nav whenever the endpoint is configured; if a visitor
  // reaches it before logging in, it explains that rather than failing.
  let draftClient: DraftClient | null = null;
  let deployClient: DeployClient | null = null;

  // Render the natural-language drafting surface (#118c): a textarea to describe an agent,
  // Corpus screen (#191): upload + browse the user's own in-scope documents. The endpoint
  // fences every read/write to the verified tenant/scope; this view just drives it.
  function showCorpus(): void {
    const outEl = document.getElementById("out");
    if (!outEl) return;
    roomPollToken += 1; // leaving any polling view stops its loop
    outEl.replaceChildren();
    if (!corpusClient) {
      renderError(outEl, "Log in to manage documents — the corpus is scoped to your access.");
      return;
    }
    renderCorpus(corpusClient, outEl);
  }

  // then the server-clamped bounded plan + a confirm step. The boundary is enforced
  // server-side — the model's draft has zero authority.
  function showDraft(): void {
    const outEl = document.getElementById("out");
    if (!outEl) return;
    roomPollToken += 1; // leaving the room view stops its poll loop
    outEl.replaceChildren();
    if (!draftClient) {
      renderError(outEl, "Log in to draft an agent — drafting runs under your own entitlements.");
      return;
    }
    const panel = document.createElement("section");
    panel.className = "panel";
    panel.setAttribute("aria-label", "Describe an agent");
    const title = document.createElement("div");
    title.className = "panel-title";
    title.textContent = "Describe an agent in plain language";
    const ta = document.createElement("textarea");
    ta.className = "field";
    ta.rows = 3;
    ta.style.cssText = "width:100%;resize:vertical";
    ta.placeholder = "e.g. an agent that summarizes new papers in my lab every Monday";
    ta.setAttribute("aria-label", "Describe the agent you want");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn";
    btn.textContent = "Draft it";
    const result = document.createElement("div");
    btn.onclick = async () => {
      const request = ta.value.trim();
      if (!request) return;
      btn.disabled = true;
      result.replaceChildren();
      result.setAttribute("aria-busy", "true");
      try {
        const plan = await draftClient!.draft(request);
        // Wire confirm to the deploy endpoint when configured; the server re-clamps the spec.
        const onConfirm = deployClient
          ? (spec: Record<string, unknown>) => deployClient!.deploy(spec)
          : undefined;
        renderDraft(plan, result, { onConfirm });
      } catch (err) {
        renderError(result, (err as Error).message);
      } finally {
        result.setAttribute("aria-busy", "false");
        btn.disabled = false;
      }
    };
    panel.append(title, ta, btn);
    outEl.append(panel, result);
  }

  // The visual builder (#117): fetch the bounded menu, render a form whose scope/tier choices
  // are ALREADY clamped to the author (unsafe is unrepresentable), then dispose the assembled
  // spec through the same compiler clamp + the #118 confirm/deploy flow.
  let authoringClient: AuthoringClient | null = null;

  async function showBuild(): Promise<void> {
    const outEl = document.getElementById("out");
    if (!outEl) return;
    roomPollToken += 1; // leaving the room view stops its poll loop
    outEl.replaceChildren();
    if (!authoringClient) {
      renderError(outEl, "Log in to build an agent — the builder is scoped to your entitlements.");
      return;
    }
    outEl.setAttribute("aria-busy", "true");
    try {
      const resp = await authoringClient.options();
      if (!resp.ok || !resp.options) {
        renderError(outEl, "Could not load the authoring options.");
        return;
      }
      renderBuilderForm(outEl, resp.options, resp.templates ?? []);
    } catch (err) {
      renderError(outEl, (err as Error).message);
    } finally {
      outEl.setAttribute("aria-busy", "false");
    }
  }

  // Build the bounded form: a scope <select> (only nodes the author holds), a capability
  // checklist, a reasoning-pattern <select>, name/description/budget fields, and an optional
  // template prefill. On "Review", dispose → renderDraft (with the deploy confirm wired).
  function renderBuilderForm(
    outEl: HTMLElement,
    options: AuthoringOptions,
    templates: TemplateRow[],
  ): void {
    const mk = (tag: string, cls = "", text?: string): HTMLElement => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined) n.textContent = text;
      return n;
    };
    const panel = mk("section", "panel");
    panel.setAttribute("aria-label", "Build an agent");
    panel.appendChild(mk("div", "panel-title", "Build an agent — bounded to what you hold"));

    const nameIn = mk("input", "field") as HTMLInputElement;
    nameIn.placeholder = "agent name (e.g. paper-sweep)";
    nameIn.setAttribute("aria-label", "Agent name");
    const descIn = mk("input", "field") as HTMLInputElement;
    descIn.placeholder = "what it does";
    descIn.setAttribute("aria-label", "Description");

    const scopeSel = mk("select", "field") as HTMLSelectElement;
    scopeSel.setAttribute("aria-label", "Scope (only nodes you hold)");
    for (const s of options.offerable_scopes) {
      const o = document.createElement("option");
      o.value = s;
      o.textContent = s || "(tenant-wide)";
      scopeSel.appendChild(o);
    }

    const patternSel = mk("select", "field") as HTMLSelectElement;
    patternSel.setAttribute("aria-label", "Reasoning pattern");
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "(default reasoning)";
    patternSel.appendChild(none);
    for (const p of options.patterns) {
      const o = document.createElement("option");
      o.value = p.key;
      o.textContent = p.key;
      patternSel.appendChild(o);
    }

    const budgetIn = mk("input", "field") as HTMLInputElement;
    budgetIn.placeholder = "budget (e.g. $20 / user / month)";
    budgetIn.setAttribute("aria-label", "Budget");

    // Capability checklist — only the catalogued tools (the menu is the allowed set).
    const toolsBox = mk("fieldset");
    toolsBox.style.cssText = "border:1px solid var(--border);border-radius:6px;padding:.5rem";
    const legend = mk("legend", "", "Capabilities");
    toolsBox.appendChild(legend);
    const toolInputs: HTMLInputElement[] = [];
    for (const c of options.capabilities) {
      const lbl = mk("label");
      lbl.style.cssText = "display:flex;gap:.4rem;align-items:center;padding:.15rem 0";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = c.name;
      toolInputs.push(cb);
      lbl.append(cb, mk("span", "", c.name + (c.description ? ` — ${c.description}` : "")));
      toolsBox.appendChild(lbl);
    }

    // Optional template prefill.
    const tmplSel = mk("select", "field") as HTMLSelectElement;
    tmplSel.setAttribute("aria-label", "Start from a template");
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "(start blank)";
    tmplSel.appendChild(blank);
    for (const t of templates) {
      const o = document.createElement("option");
      o.value = t.id;
      o.textContent = `${t.name} — ${t.description}`;
      tmplSel.appendChild(o);
    }

    const reviewBtn = mk("button", "btn", "Review") as HTMLButtonElement;
    reviewBtn.type = "button";
    const result = mk("div");

    reviewBtn.onclick = async () => {
      reviewBtn.disabled = true;
      result.replaceChildren();
      result.setAttribute("aria-busy", "true");
      try {
        const spec = buildSpecFromForm({
          agent: nameIn.value,
          description: descIn.value,
          scope: scopeSel.value,
          reasoning: patternSel.value || undefined,
          tools: toolInputs.filter((c) => c.checked).map((c) => c.value),
          budget: budgetIn.value || undefined,
        });
        const template = tmplSel.value || undefined;
        const plan = await authoringClient!.dispose(spec, template);
        const onConfirm = deployClient
          ? (s: Record<string, unknown>) => deployClient!.deploy(s)
          : undefined;
        renderDraft(plan, result, { onConfirm });
      } catch (err) {
        renderError(result, (err as Error).message);
      } finally {
        result.setAttribute("aria-busy", "false");
        reviewBtn.disabled = false;
      }
    };

    panel.append(
      mk("label", "sr-only", "Start from a template"),
      tmplSel,
      nameIn,
      descIn,
      scopeSel,
      patternSel,
      budgetIn,
      toolsBox,
      reviewBtn,
    );
    outEl.append(panel, result);
  }

  // --- Collaborative rooms (#116) -------------------------------------------
  let roomClient: RoomClient | null = null;
  let corpusClient: CorpusClient | null = null;
  // A monotonically-bumped token: each screen entry/nav bump cancels any prior poll loop, so
  // navigating away stops polling (no standing connection — NO CLOCKS on the client too).
  let roomPollToken = 0;

  async function showRoom(): Promise<void> {
    const outEl = document.getElementById("out");
    if (!outEl) return;
    roomPollToken += 1; // cancel any prior poll loop
    outEl.replaceChildren();
    if (!roomClient) {
      renderError(outEl, "Log in to use rooms — a room runs under your own entitlements.");
      return;
    }
    // A minimal room launcher: open a new room or join one by id, then enter the live view.
    const panel = document.createElement("section");
    panel.className = "panel";
    panel.setAttribute("aria-label", "Rooms");
    panel.appendChild(Object.assign(document.createElement("div"), {
      className: "panel-title",
      textContent: "Collaborative rooms",
    }));
    const idIn = document.createElement("input");
    idIn.className = "field";
    idIn.placeholder = "room id (to open or join)";
    idIn.setAttribute("aria-label", "Room id");
    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "btn";
    openBtn.textContent = "Open";
    const joinBtn = document.createElement("button");
    joinBtn.type = "button";
    joinBtn.className = "btn ghost";
    joinBtn.textContent = "Join";
    const live = document.createElement("div");

    openBtn.onclick = async () => {
      const v = await roomClient!.open(idIn.value.trim() || "room");
      if (!v.ok) return renderError(live, v.reason || "could not open room");
      enterRoom(v.room, live);
    };
    joinBtn.onclick = async () => {
      const id = idIn.value.trim();
      if (!id) return;
      const v = await roomClient!.join(id);
      if (!v.ok) return renderError(live, v.reason || "could not join room");
      enterRoom(v.room, live);
    };
    panel.append(idIn, openBtn, joinBtn);
    outEl.append(panel, live);
  }

  // Enter the live room view: a members panel, the message stream, a composer, and a poll loop
  // folding new messages. The loop self-cancels when `roomPollToken` is bumped (nav away).
  function enterRoom(roomId: string, target: HTMLElement): void {
    const myToken = (roomPollToken += 1);
    target.replaceChildren();
    const membersEl = document.createElement("div");
    const streamEl = document.createElement("div");
    const composer = document.createElement("div");
    composer.className = "input-bar";
    const text = document.createElement("input");
    text.className = "field";
    text.placeholder = "message the room…";
    text.setAttribute("aria-label", "Message");
    const send = document.createElement("button");
    send.type = "button";
    send.className = "btn";
    send.textContent = "Send";
    const note = document.createElement("p");
    note.className = "cost-line";
    composer.append(text, send);
    target.append(membersEl, streamEl, composer, note);

    send.onclick = async () => {
      const t = text.value.trim();
      if (!t) return;
      send.disabled = true;
      const r = await roomClient!.postMessage(roomId, t);
      if (r.ok) {
        text.value = "";
        note.textContent = "";
      } else {
        // a budget/membership rejection is surfaced plainly (the gate working, not an error).
        note.textContent = r.reason || "message rejected";
      }
      send.disabled = false;
    };

    // Poll loop: fetch the full view (members + messages) every ~2s; stop if the token moved.
    const tick = async () => {
      if (myToken !== roomPollToken) return; // cancelled (navigated away / re-entered)
      try {
        const v = await roomClient!.events(roomId, 0);
        if (myToken !== roomPollToken) return;
        if (v.ok) {
          renderMembers(v, membersEl);
          renderMessages(v.messages, streamEl);
        }
      } catch {
        /* transient; the next tick retries */
      }
      if (myToken === roomPollToken) setTimeout(tick, 2000);
    };
    void tick();
  }

  const scopeEl = document.getElementById("scope")!;
  const form = document.getElementById("f") as HTMLFormElement;

  if (!config.brokerUrl) {
    scopeEl.textContent =
      "Set VITE_BROKER_URL (and VITE_AGENT_RUNTIME_ARN for Panel/Analyze) to enable chat.";
    authBtn.style.display = "none";
    return;
  }

  // Login gate. With the Hosted UI wired, an unauthenticated visitor sees only a
  // "Log in" button; the chat form is disabled until they have a token.
  const loggedIn = isLoggedIn();
  if (loginConfig) {
    authBtn.textContent = loggedIn ? "Log out" : "Log in";
    authBtn.onclick = () => (loggedIn ? logout(loginConfig) : login(loginConfig));
  } else {
    authBtn.style.display = "none";
  }
  if (!loggedIn) {
    scopeEl.textContent = loginConfig
      ? "Log in to start — you'll get a session scoped to your entitlements."
      : "No token: append #idp_token=<jwt> to the URL, or wire VITE_COGNITO_DOMAIN for a login button.";
    form.querySelectorAll("input,select,button").forEach((el) => ((el as HTMLInputElement).disabled = true));
    return;
  }

  const creds = new CredentialManager(config.brokerUrl, () => Promise.resolve(idpToken()));
  // Tier-0 "Ask" transport. DEFAULT (no chokepoint configured) = browser-direct Bedrock
  // (works from a CLI/native caller; from a web origin it's blocked by Bedrock's lack of CORS).
  // When `chokepointUrl` is set, Ask routes through the OPTIONAL Tier-1 choke point instead: a
  // gated, metered, server-enforced call that assumes the user's OWN scoped role + runs the
  // pre-call budget cascade — the same boundary every other agate surface funnels through, and
  // CORS-reachable from the browser. (Panel/Analyze always go through the AgentCore Runtime.)
  const askTransport: Transport = config.chokepointUrl
    ? new OpenAITransport(
        {
          region: config.region,
          endpoint: config.chokepointUrl,
          scope: () => {
            const s = creds.scope;
            return {
              tenant: s?.tenant ?? "",
              user: idpToken() ? "self" : "",  // server derives the real subject from the token
              period: "",  // server stamps the current period; not client-trusted
              tier: s?.tier ?? "oss",
              courses: s?.courses ?? [],
            };
          },
        },
        () => creds.get(),
        () => idpToken(),
      )
    : new BedrockTransport(config.region, () => creds.get(), () => {
        // Attribution for the spend meter (#77): tenant/user from the session scope.
        const s = creds.scope;
        return s ? { "agate:tenant": s.tenant, "agate:affiliation": s.affiliation } : undefined;
      });
  const agent = config.agentRuntimeArn
    ? new AgentCoreTransport({ region: config.region, runtimeArn: config.agentRuntimeArn }, () => creds.get())
    : null;
  // The drafting client (#118c) — SigV4-signs the drafting Function URL with the scoped
  // creds; the endpoint clamps the model's draft to the verified author authority.
  if (config.draftingUrl) {
    draftClient = new DraftClient(
      { region: config.region, endpoint: config.draftingUrl },
      () => creds.get(),
      () => idpToken(),
    );
  }
  // The deploy-on-confirm client (#118) — POSTs the confirmed spec; the endpoint re-clamps it
  // server-side and persists the governed record.
  if (config.deployUrl) {
    deployClient = new DeployClient(
      { region: config.region, endpoint: config.deployUrl },
      () => creds.get(),
      () => idpToken(),
    );
  }
  // The corpus client (#191) — upload + list the user's in-scope documents; the endpoint
  // fences every read/write to the verified tenant/scope.
  if (config.corpusUrl) {
    corpusClient = new CorpusClient(
      { region: config.region, endpoint: config.corpusUrl },
      () => creds.get(),
      () => idpToken(),
    );
  }
  // The graphical-authoring client (#117) — fetches the bounded menu + disposes the assembled
  // spec; the menu is pre-clamped server-side and the dispose re-clamps.
  if (config.authoringUrl) {
    authoringClient = new AuthoringClient(
      { region: config.region, endpoint: config.authoringUrl },
      () => creds.get(),
      () => idpToken(),
    );
  }
  // The collaborative-rooms client (#116) — open/join/leave/post/poll; the room's reach is the
  // server-enforced intersection of members, every message attributed + budget-gated.
  if (config.roomsUrl) {
    roomClient = new RoomClient(
      { region: config.region, endpoint: config.roomsUrl },
      () => creds.get(),
      () => idpToken(),
    );
  }

  const out = document.getElementById("out")!;
  const mainCol = document.getElementById("main")!;
  const input = document.getElementById("q") as HTMLTextAreaElement;
  const modeSel = document.getElementById("mode") as HTMLSelectElement;
  const modelSel = document.getElementById("model") as HTMLSelectElement;
  const emptyState = document.getElementById("empty");

  // A shared retriever for Ask grounding (created once if RAG is wired).
  const retrieverForGrounding = config.retrievalProxyUrl
    ? new Retriever(
        { region: config.region, endpoint: config.retrievalProxyUrl },
        () => creds.get(),
        () => idpToken(),
      )
    : null;
  // Cross-session memory client (#194), opt-in (only when VITE_MEMORY_URL set + the
  // billable agate-memory stack deployed). recall folds into grounding; record fires after
  // a turn. Namespaces are server-derived from the verified token.
  const memoryClient = config.memoryUrl
    ? new MemoryClient(
        { region: config.region, endpoint: config.memoryUrl },
        () => creds.get(),
        () => idpToken(),
      )
    : null;
  // `chats` is created below; the grounding provider reads the active chat's sessionId
  // lazily (the provider only runs per-turn, after `chats` exists).
  let chats: ChatManager;
  // Capture the last turn's retrieved chunks (for the Sources footer) — the context
  // provider runs inside ChatSession.send, so we stash them here per turn.
  let lastSources: RetrievedChunk[] = [];
  // The grounding provider folds BOTH RAG chunks and recalled memory into the turn's
  // prepended context (mirrors how the agent path folds memory into `evidence`). Present
  // when either RAG or memory is wired.
  const groundingProvider =
    retrieverForGrounding || memoryClient
      ? async (query: string) => {
          const messages: import("./transport").ChatMessage[] = [];
          if (retrieverForGrounding) {
            lastSources = await retrieverForGrounding.retrieve(query);
            messages.push(...withContext([], lastSources));
          } else {
            lastSources = [];
          }
          if (memoryClient) {
            const remembered = await memoryClient.recall({
              tier: "personal",
              query,
              sessionId: chats.current.sessionId,
            });
            if (remembered) messages.unshift({ role: "system", content: remembered });
          }
          return messages;
        }
      : undefined;

  // Context-usage gauge (#2): show how full the active chat's context window is.
  const ctxBar = document.getElementById("ctx-bar")!;
  const ctxText = document.getElementById("ctx-text")!;
  const renderContext = (chat: { contextTokens: number; turns: number }, windowTokens: number) => {
    const pct = windowTokens > 0 ? Math.min(100, (chat.contextTokens / windowTokens) * 100) : 0;
    ctxBar.style.width = `${pct.toFixed(1)}%`;
    const wrap = ctxBar.parentElement!.parentElement!;
    wrap.dataset.level = pct >= 90 ? "high" : pct >= 70 ? "mid" : "ok";
    ctxText.textContent = chat.contextTokens
      ? `${chat.contextTokens.toLocaleString()} / ${windowTokens.toLocaleString()} tokens · ${Math.round(pct)}% · ${chat.turns} turn${chat.turns === 1 ? "" : "s"}`
      : "empty · new chat";
  };

  // Multi-session manager (#1): each chat is an independent conversation with its own
  // transcript DOM + ChatSession (multi-turn history) + token tally. "New chat" starts
  // fresh; the sidebar list switches between them. Transcripts render into #out; the
  // main COLUMN scrolls (the composer flows beneath).
  // Remember which chats we've already shown the memory seed for, so switching back and
  // forth doesn't re-recall (a billable op).
  const seededChats = new Set<number>();
  chats = new ChatManager({
    appendHost: out,
    scrollHost: mainCol,
    listHost: document.getElementById("chat-list")!,
    transport: askTransport,
    contextProvider: groundingProvider,
    onActiveChange: (chat) => {
      renderContext(chat, contextWindowFor(chat.modelId));
      if (emptyState) emptyState.hidden = chat.turns > 0;
      // Memory seed (#194 follow-up): on first view of an EMPTY chat, recall the caller's
      // personal memory once and show what the assistant remembers — so continuity is
      // visible before the first question. Best-effort, billable-op-aware (once per chat).
      if (memoryClient && chat.turns === 0 && !seededChats.has(chat.id)) {
        seededChats.add(chat.id);
        void memoryClient
          .recall({ tier: "personal", query: "", sessionId: chat.sessionId })
          .then((remembered) => {
            if (remembered && chat.turns === 0) renderMemorySeed(remembered);
          });
      }
    },
  });
  document.getElementById("new-chat")?.addEventListener("click", () => {
    chats.newChat(modelSel?.value && modelSel.value !== AUTO ? modelSel.value : undefined);
    input.focus();
  });

  const meter = new SessionMeter({
    total: document.getElementById("cost")!,
    status: document.getElementById("cost-status")!,
    budgetWrap: document.getElementById("budget")!,
    budgetBar: document.getElementById("budget-bar")!,
    budgetText: document.getElementById("budget-text")!,
  });

  // Show the verified scope as chips once known (and refresh after the first vend).
  const showScope = () => {
    const s = creds.scope;
    if (s) renderScopeChips(s);
  };
  showScope();

  // Textarea auto-grow + Enter-to-send (Shift+Enter for a newline), like a chatbot.
  const autoGrow = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  };
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  // Suggestion chips. Initially a few entitlement-neutral sample questions; after an
  // answer (when the follow-ups toggle is on) these are replaced with model-suggested
  // follow-ups. Clicking a chip fills the box and sends it. Hidden in Panel/Analyze.
  const SAMPLE_QUESTIONS = [
    "Summarize the key points in my documents.",
    "What does the first law of thermodynamics state?",
    "What topics do my documents cover?",
  ];
  const chipsHost = document.getElementById("chips");
  const followupsToggle = document.getElementById("followups-toggle") as HTMLInputElement | null;
  const followupsCost = document.getElementById("followups-cost");
  // Replace the chips, fading the new set in (each chip animates via CSS). Clears the
  // `fading` class so the group is visible.
  const setChips = (questions: string[]): void => {
    if (!chipsHost) return;
    chipsHost.classList.remove("fading");
    chipsHost.replaceChildren(
      ...questions.map((text) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip";
        chip.textContent = text;
        chip.addEventListener("click", () => {
          input.value = text;
          autoGrow();
          form.requestSubmit();
        });
        return chip;
      }),
    );
  };
  // Fade the current chips out immediately (on submit) — new ones fade in when ready.
  const fadeChips = (): void => chipsHost?.classList.add("fading");
  setChips(SAMPLE_QUESTIONS);

  // Running totals for the (opt-in) follow-up suggestions, shown in the Suggestions
  // box. Separate from the main meter line, but ALSO folded into the running cost.
  const followupsRunning = { cost: 0, inTok: 0, outTok: 0 };
  const renderFollowupsCost = (): void => {
    if (!followupsCost) return;
    if (followupsRunning.cost <= 0) {
      followupsCost.hidden = true;
      return;
    }
    const t = followupsRunning;
    followupsCost.textContent =
      `Suggestions this session: $${t.cost.toFixed(6)} · ` +
      `${t.inTok.toLocaleString()} in / ${t.outTok.toLocaleString()} out`;
    followupsCost.hidden = false;
  };

  // Populate the model picker with the session's ENTITLED models (Auto + each model the
  // tier permits). The picker never lists an unentitled model, so a user can't pin past
  // their tier; the server-side router (#122) clamps to entitlement + budget regardless.
  function populateModels(tier: Tier | undefined): void {
    if (!tier) return;
    const opts = modelOptions(tier);
    modelSel.replaceChildren(
      ...opts.map((o) => {
        const el = document.createElement("option");
        el.value = o.value;
        el.textContent = o.label;
        return el;
      }),
    );
  }
  populateModels(creds.scope?.tier);
  // creds.scope is filled after the first vend; refresh the picker + chips once available.
  void creds
    .get()
    .then(() => {
      populateModels(creds.scope?.tier);
      showScope();
    })
    .catch(() => {});

  // Chips only make sense for Ask; hide them in Panel/Analyze/pattern modes. The
  // placeholder verb also tracks the mode ("Ask a question…" / "Run a panel…" / …).
  const PLACEHOLDERS: Record<string, string> = {
    ask: "Ask a question…",
    panel: "Pose a question for the panel…",
    analyze: "Describe an analysis to run…",
  };
  const syncModeUi = () => {
    const mode = modeSel.value;
    if (chipsHost) chipsHost.hidden = mode !== "ask";
    input.placeholder = mode.startsWith("pattern:")
      ? "Pose a question for this reasoning pattern…"
      : (PLACEHOLDERS[mode] ?? "Ask a question…");
  };
  modeSel.addEventListener("change", syncModeUi);
  syncModeUi();

  // --- Notebook view (#185) -------------------------------------------------
  // A view of the current chat: the transcript projected into editable prompt cells, each
  // re-runnable as a STANDALONE metered call (not a ChatSession turn, so it never pollutes
  // the transcript). The context gauge stays chat-scoped (cell runs don't touch history).
  const resolvePin = (): string => (modelSel.value === AUTO ? AUTO : modelSel.value);
  const paintNotebook = (): void => {
    const chat = chats.current;
    const nb = chats.notebookFor(chat);
    renderNotebook(nb, chat.notebookEl, {
      onRun: (cellId, prompt) => void runNotebookCell(cellId, prompt),
      onAddCell: () => {
        nb.cells.push(newCell());
        paintNotebook();
        const last = chat.notebookEl.querySelector<HTMLTextAreaElement>(
          ".notebook-cell:last-of-type .notebook-cell-prompt",
        );
        last?.focus();
      },
    });
  };
  const runNotebookCell = async (cellId: string, prompt: string): Promise<void> => {
    const chat = chats.current;
    const nb = chats.notebookFor(chat);
    const cell = nb.cells.find((c: NotebookCell) => c.id === cellId);
    if (!cell || !prompt.trim()) return;
    cell.prompt = prompt;
    cell.state = "running";
    cell.error = undefined;
    paintNotebook();
    try {
      const result = await runCell(askTransport, resolvePin(), prompt, groundingProvider);
      cell.answer = result.text;
      cell.sources = lastSources.slice();
      cell.meta = {
        usage: result.usage,
        cost: result.cost,
        budget: result.budget,
        modelId: result.model ?? (resolvePin() === AUTO ? undefined : resolvePin()),
        modelReason: result.modelRoute?.reason,
      };
      cell.state = "idle";
      meter.record(result.cost, result.budget); // same fold-in as a chat turn
    } catch (err) {
      cell.state = "error";
      cell.error = (err as Error).message;
    }
    paintNotebook();
  };
  const setView = (view: "chat" | "notebook"): void => {
    chats.setView(chats.current.id, view);
    document.querySelectorAll<HTMLButtonElement>(".view-btn").forEach((b) => {
      const on = b.dataset.view === view;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", String(on));
    });
    // The composer belongs to the chat view; hide it in the notebook (cells run themselves).
    form.hidden = view === "notebook";
    if (chipsHost) chipsHost.hidden = view === "notebook" || modeSel.value !== "ask";
    if (emptyState) emptyState.hidden = view === "notebook" || chats.current.turns > 0;
    if (view === "notebook") paintNotebook();
  };
  document.querySelectorAll<HTMLButtonElement>(".view-btn").forEach((b) => {
    b.addEventListener("click", () => setView((b.dataset.view as "chat" | "notebook") ?? "chat"));
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    autoGrow();
    if (emptyState) emptyState.hidden = true;
    // Fade the suggestion chips out the moment a question is submitted; the new set
    // fades back in once the answer (and any follow-ups) settle.
    fadeChips();
    const selected = modeSel.value; // "ask"|"panel"|"analyze" or "pattern:<key>"
    out.setAttribute("aria-busy", "true");
    const submitBtn = form.querySelector("button[type=submit]") as HTMLButtonElement;
    submitBtn.disabled = true;

    try {
      const pattern = selected.startsWith("pattern:") ? selected.slice("pattern:".length) : null;
      // Resolve the model: a chosen entitled model wins; "auto" sends the literal
      // "auto" so the SERVER routes within the verified tier + budget (#190). The
      // picker only lists entitled models, so a pin can't escape the tier either.
      const pin = modelSel.value === AUTO ? AUTO : modelSel.value;
      if (!pattern && selected === "ask") {
        // The active chat's stable id for memory (#194) — captured before the turn so a
        // mid-turn chat switch can't misattribute the record.
        const askSessionId = chats.current.sessionId;
        await runAsk(q, chats, meter, () => lastSources, pin, (question, answer, m) => {
          // Cross-session memory (#194), opt-in: record the finished turn so a future
          // session can recall it. Fire-and-forget; namespace is server-derived.
          if (memoryClient) {
            void memoryClient.record({
              sessionId: askSessionId,
              payload: [
                { role: "user", text: question },
                { role: "assistant", text: answer },
              ],
            });
          }
          // Dynamic follow-up chips (opt-in). Generate after the answer; on failure
          // or empty result, fall back to the sample questions. Fire-and-forget so it
          // never blocks the UI. It's a real metered call (same choke point), so fold
          // its cost into the session meter AND report it in the Suggestions box.
          if (!followupsToggle?.checked) {
            setChips(SAMPLE_QUESTIONS); // toggle off → just restore the samples
            return;
          }
          void suggestFollowups(askTransport, m, question, answer).then((r) => {
            setChips(r.questions.length ? r.questions : SAMPLE_QUESTIONS);
            // The suggestion call is billed too: fold it into the running cost meter
            // and accumulate the Suggestions-box running total (cost + tokens).
            meter.record(r.cost, r.budget);
            if (typeof r.cost === "number") followupsRunning.cost += r.cost;
            if (r.usage) {
              followupsRunning.inTok += r.usage.inputTokens;
              followupsRunning.outTok += r.usage.outputTokens;
            }
            renderFollowupsCost();
          });
        });
      } else {
        if (!agent) {
          renderError(out, "Panel/Analyze/patterns need VITE_AGENT_RUNTIME_ARN (the deployed agent).");
          return;
        }
        // A pattern run sends {pattern}; a plain mode sends {mode}.
        await runAgent(q, pattern ? { pattern } : { mode: selected as UiMode }, agent, out);
      }
      showScope();
    } catch (err) {
      renderError(out, (err as Error).message);
    } finally {
      out.setAttribute("aria-busy", "false");
      submitBtn.disabled = false;
      input.focus();
    }
  });
}

// --- Ask (Tier 0, streamed) -------------------------------------------------

async function runAsk(
  q: string,
  chats: ChatManager,
  meter: SessionMeter,
  getSources: () => RetrievedChunk[], // the chunks the grounding provider last fetched
  modelId?: string,
  onAnswered?: (question: string, answer: string, modelId: string) => void,
): Promise<void> {
  const turn = chats.current.transcript.begin(q);

  // The requested model: a pin, "auto" (server routes within tier+budget, #190), or
  // the configured default. The active chat's ChatSession carries the multi-turn
  // history; rebuilt only if the requested model changed (so switching keeps history).
  const requested = modelId ?? config.defaultModelId;
  const session = chats.sessionFor(requested);
  // Stream raw text live (so the user sees progress immediately), then render the
  // accumulated answer as Markdown + math once the stream completes — re-rendering
  // mid-stream would repeatedly try to typeset half-finished formulae.
  try {
    const result = await session.send(q, {
      onReasoning: () => turn.thinking(),
      onDelta: (d) => turn.appendDelta(d),
    });
    // The server reports which model actually ran (esp. under "auto"); show that,
    // with the routing rationale as a tooltip when present.
    const ranModel = result.model ?? (requested === AUTO ? undefined : requested);
    turn.finalize(result.text, getSources(), {
      usage: result.usage,
      cost: result.cost,
      budget: result.budget,
      modelId: ranModel,
      modelReason: result.modelRoute?.reason,
    });
    meter.record(result.cost, result.budget);
    if (result.text.trim()) {
      chats.recordTurn(q, result.text);
      // Follow-ups need a concrete model id, not "auto" — use the one that ran.
      onAnswered?.(q, result.text, ranModel ?? config.defaultModelId);
    }
  } catch (err) {
    turn.fail((err as Error).message);
    throw err;
  }
}

// --- Panel / Analyze (agent path, event stream -> panes) --------------------

async function runAgent(
  q: string,
  choice: { mode: UiMode } | { pattern: string },
  agent: AgentCoreTransport,
  out: HTMLElement,
): Promise<void> {
  let state: RunState = emptyRunState();
  const panel = document.createElement("div");
  const cells = document.createElement("div");
  out.append(panel, cells);

  const costEl = document.getElementById("cost");
  const repaint = () => {
    // renderPanel draws one column per model pane PLUS the reconciliation
    // (divergence) column when present, so panes + divergence render together.
    if (state.panes.length || state.divergence) renderPanel(state, panel);
    if (state.cells.length) renderCells(state.cells, cells);
    // The agent path reports its own running total (no budget cascade); show it in
    // the same meter. (Ask uses the SessionMeter for per-call cost + budget.)
    if (costEl) costEl.textContent = `$${(state.costTotal || 0).toFixed(4)}`;
  };

  const emit = (ev: RunEvent) => {
    state = reduce(state, ev);
    repaint();
  };

  await agent.run(
    {
      question: q,
      idp_token: idpToken(), // verified server-side; SPA never sends a tier
      ...("pattern" in choice
        ? { pattern: choice.pattern }
        : { mode: uiToRoute(choice.mode) }),
    },
    emit,
  );
  repaint();
}

// Capture any token from a login redirect fragment (and scrub the URL) before the
// first render, so isLoggedIn() reflects a just-completed login.
currentToken();
main();
