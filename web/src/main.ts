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
import { reduce, type RunState, emptyRunState } from "./events/collector";
import type { RunEvent } from "./events/protocol";
import { renderCells, renderPanel } from "./panes/render";
import { withContext } from "./rag/context";
import { Retriever } from "./rag/retriever";
import { AgentCoreTransport } from "./transport/agentcore";
import { BedrockTransport } from "./transport/bedrock";
import { type UiMode, UI_MODES, uiToRoute } from "./router";

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
  const { topbar } = mountChrome({
    brand: "agate",
    tag: "GenAI gateway",
    actions: [authBtn],
    items: navItems,
  });
  app.insertBefore(topbar, app.firstChild);

  function selectMode(value: string): void {
    const sel = document.getElementById("mode") as HTMLSelectElement | null;
    if (sel) sel.value = value;
    (document.getElementById("q") as HTMLInputElement | null)?.focus();
  }

  // Render the governed-access console into the main region. Admin-gated server-side.
  async function showAdmin(): Promise<void> {
    const out = document.getElementById("out");
    if (!out) return;
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

  const out = document.getElementById("out")!;
  const input = document.getElementById("q") as HTMLInputElement;
  const modeSel = document.getElementById("mode") as HTMLSelectElement;

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
      if (!pattern && selected === "ask") {
        await runAsk(q, bedrock, creds, out);
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
  const session = new ChatSession(bedrock, config.defaultModelId, undefined, undefined, contextProvider);
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
