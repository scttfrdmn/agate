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

import { CredentialManager } from "./auth/credentials";
import { currentToken, isLoggedIn, login, logout, type LoginConfig } from "./auth/login";
import { ChatSession } from "./chat/session";
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
  app.innerHTML = `
    <main style="max-width:64rem;margin:1.5rem auto;font-family:system-ui">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <h1 style="margin:0">agate</h1>
        <button id="auth" style="padding:.35rem .75rem"></button>
      </div>
      <p id="scope" style="color:#666;margin:.25rem 0 1rem"></p>
      <form id="f" style="display:flex;gap:.5rem;align-items:center">
        <select id="mode" style="padding:.5rem">
          ${UI_MODES.map((m) => `<option value="${m.value}">${m.label}</option>`).join("")}
        </select>
        <input id="q" style="flex:1;padding:.5rem" placeholder="Ask a research question…" autocomplete="off" />
        <button>Send</button>
      </form>
      <div id="out" style="margin-top:1rem"></div>
      <p id="cost" style="color:#888;font-size:.85em;margin-top:.5rem"></p>
    </main>`;
}

function showCost(total: number): void {
  document.getElementById("cost")!.textContent = total ? `running cost: $${total.toFixed(4)}` : "";
}

function main(): void {
  const app = document.getElementById("app");
  if (!app) return;
  render(app);

  const scopeEl = document.getElementById("scope")!;
  const form = document.getElementById("f") as HTMLFormElement;
  const authBtn = document.getElementById("auth") as HTMLButtonElement;

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
  const bedrock = new BedrockTransport(config.region, () => creds.get());
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
    const mode = modeSel.value as UiMode;
    out.replaceChildren();

    try {
      if (mode === "ask") {
        await runAsk(q, bedrock, creds, out);
      } else {
        if (!agent) {
          out.textContent = "Panel/Analyze need VITE_AGENT_RUNTIME_ARN (the deployed agent).";
          return;
        }
        await runAgent(q, mode, agent, out);
      }
      const s = creds.scope;
      if (s) {
        document.getElementById("scope")!.textContent =
          `tier=${s.tier} · tenant=${s.tenant} · ${s.affiliation}`;
      }
    } catch (err) {
      out.textContent = `[error: ${(err as Error).message}]`;
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
  log.style.cssText = "white-space:pre-wrap;border:1px solid #ddd;padding:1rem;min-height:6rem";
  out.appendChild(log);
  log.textContent = `> ${q}\n`;

  // RAG grounding when a vector store is configured (scoped to the session's tenant).
  let contextProvider;
  if (config.vectorBucketName) {
    contextProvider = async (query: string) => {
      await creds.get();
      const tenant = creds.scope?.tenant;
      if (!tenant) return [];
      const retriever = new Retriever(
        { region: config.region, vectorBucketName: config.vectorBucketName, indexName: `agate-${tenant}` },
        () => creds.get(),
      );
      return withContext([], await retriever.retrieve(query));
    };
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
  mode: UiMode,
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
      mode: uiToRoute(mode),
    },
    emit,
  );
  repaint();
}

// Capture any token from a login redirect fragment (and scrub the URL) before the
// first render, so isLoggedIn() reflects a just-completed login.
currentToken();
main();
