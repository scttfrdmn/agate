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
import "./styles/agate.css";

import { fetchAdmin, renderAdmin } from "./admin/view";
import { CredentialManager } from "./auth/credentials";
import { currentToken, isLoggedIn, login, logout, type LoginConfig } from "./auth/login";
import { ChatSession } from "./chat/session";
import { mountChrome } from "./chrome/nav";
import { config } from "./config";
import { type AuthoringOptions, AuthoringClient, type TemplateRow } from "./drafting/builder";
import { buildSpecFromForm } from "./drafting/builder";
import { DeployClient, DraftClient, renderDraft } from "./drafting/draft";
import { RoomClient } from "./rooms/client";
import { renderMembers, renderMessages } from "./rooms/view";
import { reduce, type RunState, emptyRunState } from "./events/collector";
import type { RunEvent } from "./events/protocol";
import { renderCells, renderPanel } from "./panes/render";
import { withContext } from "./rag/context";
import { Retriever } from "./rag/retriever";
import { AgentCoreTransport } from "./transport/agentcore";
import { BedrockTransport } from "./transport/bedrock";
import { AUTO, type Tier, type UiMode, UI_MODES, modelOptions, uiToRoute } from "./router";

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
      redirectUri: location.origin + location.pathname,
    }
  : null;

function render(app: HTMLElement): void {
  // Semantic landmarks (header / main / aside) + labelled controls + an ARIA
  // live region so screen-reader users hear the streamed answer and run progress.
  app.innerHTML = `
    <div class="layout">
      <header class="app-header">
        <div>
          <h1>agate</h1>
          <p class="subtitle">AWS-native GenAI gateway · governed by your campus identity</p>
        </div>
      </header>

      <main id="main" class="main-col" tabindex="-1">
        <p id="scope" class="cost-line" role="status" aria-live="polite"></p>

        <form id="f" class="composer" aria-label="Ask agate">
          <div class="field">
            <label for="mode">Mode</label>
            <select id="mode">
              ${UI_MODES.map((m) => `<option value="${m.value}">${m.label}</option>`).join("")}
              <optgroup label="Reasoning patterns">
                <option value="pattern:lit-review">Pattern · Literature synthesis</option>
                <option value="pattern:red-team">Pattern · Steel-man / red-team</option>
              </optgroup>
            </select>
          </div>
          <div class="field">
            <label for="model">Model</label>
            <select id="model" title="Auto routes within your entitlement + budget; or pin a model">
              <option value="auto">Auto (entitlement-aware)</option>
            </select>
          </div>
          <div class="field" style="flex:1">
            <label for="q">Your question</label>
            <div class="input-bar">
              <input id="q" type="text" placeholder="Ask a research question…" autocomplete="off"
                     aria-describedby="scope" />
            </div>
          </div>
          <button class="btn" type="submit">Send</button>
        </form>

        <!-- The run output. aria-live=polite announces streamed answer + panes;
             aria-busy is toggled while a run is in flight. -->
        <section id="out" class="answer-region" aria-live="polite" aria-atomic="false"
                 aria-label="Answer"></section>
      </main>

      <aside class="sidebar" aria-label="Session">
        <div class="panel">
          <div class="panel-title">Running cost</div>
          <div id="cost" class="meter-total" aria-live="polite">$0.0000</div>
          <div class="meter-status">this session · billed per request</div>
        </div>
      </aside>
    </div>`;
}

function showCost(total: number): void {
  document.getElementById("cost")!.textContent = `$${(total || 0).toFixed(4)}`;
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
  const bedrock = new BedrockTransport(config.region, () => creds.get(), () => {
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
  const input = document.getElementById("q") as HTMLInputElement;
  const modeSel = document.getElementById("mode") as HTMLSelectElement;
  const modelSel = document.getElementById("model") as HTMLSelectElement;

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
  // creds.scope is filled after the first vend; refresh the picker once available.
  void creds.get().then(() => populateModels(creds.scope?.tier)).catch(() => {});

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    const selected = modeSel.value; // "ask"|"panel"|"analyze" or "pattern:<key>"
    out.replaceChildren();
    out.setAttribute("aria-busy", "true");
    const submitBtn = form.querySelector("button[type=submit]") as HTMLButtonElement;
    submitBtn.disabled = true;

    try {
      const pattern = selected.startsWith("pattern:") ? selected.slice("pattern:".length) : null;
      // Resolve the model pin: a chosen entitled model wins; "auto" (or anything not
      // in the picker's entitled list) falls back to routing/default. The picker only
      // ever lists entitled models, so this can't escape the tier.
      const pin = modelSel.value === AUTO ? undefined : modelSel.value;
      if (!pattern && selected === "ask") {
        await runAsk(q, bedrock, creds, out, pin);
      } else {
        if (!agent) {
          renderError(out, "Panel/Analyze/patterns need VITE_AGENT_RUNTIME_ARN (the deployed agent).");
          return;
        }
        // A pattern run sends {pattern}; a plain mode sends {mode}.
        await runAgent(q, pattern ? { pattern } : { mode: selected as UiMode }, agent, out);
      }
      const s = creds.scope;
      if (s) {
        document.getElementById("scope")!.textContent =
          `tier=${s.tier} · tenant=${s.tenant} · ${s.affiliation}`;
      }
    } catch (err) {
      renderError(out, (err as Error).message);
    } finally {
      out.setAttribute("aria-busy", "false");
      submitBtn.disabled = false;
    }
  });
}

// --- Ask (Tier 0, streamed) -------------------------------------------------

async function runAsk(
  q: string,
  bedrock: BedrockTransport,
  creds: CredentialManager,
  out: HTMLElement,
  modelId?: string,
): Promise<void> {
  const log = document.createElement("div");
  log.className = "answer-log";
  out.appendChild(log);
  log.textContent = `> ${q}\n`;

  // RAG grounding via the broker-proxied retriever (#84). The proxy derives the
  // tenant + scope filter from the verified token; this client supplies only the
  // query. Tenant/scope are NOT taken from anything the browser controls.
  let contextProvider;
  if (config.retrievalProxyUrl) {
    const retriever = new Retriever(
      { region: config.region, endpoint: config.retrievalProxyUrl },
      () => creds.get(),
      () => idpToken(),
    );
    contextProvider = async (query: string) => withContext([], await retriever.retrieve(query));
  }
  // A pinned (entitled) model wins; else the configured default (the server-side
  // entitlement-aware router, #122, will refine this once wired into the live path).
  const session = new ChatSession(
    bedrock, modelId ?? config.defaultModelId, undefined, undefined, contextProvider,
  );
  await session.send(q, {
    onReasoning: () => (log.textContent += log.textContent.includes("[thinking…]") ? "" : "[thinking…] "),
    onDelta: (d) => (log.textContent += d),
  });
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

  const repaint = () => {
    // renderPanel draws one column per model pane PLUS the reconciliation
    // (divergence) column when present, so panes + divergence render together.
    if (state.panes.length || state.divergence) renderPanel(state, panel);
    if (state.cells.length) renderCells(state.cells, cells);
    showCost(state.costTotal);
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
