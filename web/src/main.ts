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

import { CredentialManager } from "./auth/credentials";
import { currentToken, identityFromToken, isLoggedIn, login, logout, type LoginConfig } from "./auth/login";
import { mountChrome } from "./chrome/nav";
import { config } from "./config";
import { MemoryClient } from "./memory/client";
import { reduce, type RunState, emptyRunState } from "./events/collector";
import type { RunEvent } from "./events/protocol";
import { renderCells, renderPanel } from "./panes/render";
import { ChatManager } from "./chat/manager";
import { ScrollAnchor } from "./chat/scroll";
import { SessionMeter } from "./chat/meter";
import { suggestFollowups } from "./chat/followups";
import { type AgentCap, type NotebookCell, isEditedSinceRun, newCell } from "./chat/notebook";
import { renderNotebook } from "./chat/notebook-ui";
import { runAgentCell, runCell } from "./chat/notebook-run";
import { dependentsOf, nextCellName, resolveSource, resolveSourceWithImages } from "./chat/dag";
import { deserializeNotebook, serializeNotebook } from "./chat/notebook-store";
import { CodeKernel } from "./notebook/kernel";
import { renderError, renderMemorySeed, renderScopeChips } from "./app/dom";
import { renderShell } from "./app/shell";
import { createScreens } from "./features/screens";
import { type RetrievedChunk, withContext } from "./rag/context";
import { Retriever } from "./rag/retriever";
import { AgentCoreTransport } from "./transport/agentcore";
import { BedrockTransport } from "./transport/bedrock";
import { OpenAITransport } from "./transport/openai";
import type { BudgetStatus, Transport } from "./transport";
import {
  AUTO,
  type Tier,
  type UiMode,
  contextWindow as contextWindowFor,
  entitledModels,
  modelOptions,
  resolveModelPin,
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

function main(): void {
  const app = document.getElementById("app");
  if (!app) return;
  renderShell(app);

  // The auth (login/logout) control lives in the shared top bar, with a "Signed in as <name>"
  // label beside it so it's always clear WHO the session belongs to (the scope chips in the
  // sidebar show tier/tenant/role; this shows the identity).
  const whoami = document.createElement("span");
  whoami.className = "whoami";
  whoami.hidden = true;
  const authBtn = document.createElement("button");
  authBtn.type = "button";
  authBtn.className = "btn ghost";

  // Shared credential manager (constructor is side-effect-free — no fetch until first use) and
  // the pop-out feature screens controller (#221). Created early so the nav can reference its
  // handlers; its feature clients stay null until `screens.initClients()` runs after login, so a
  // logged-out click still shows the screen's "log in first" message.
  const creds = new CredentialManager(config.brokerUrl, () => Promise.resolve(idpToken()));
  const screens = createScreens({ idpToken, creds });

  // Top bar + pop-out navigation. The Admin item is offered whenever the console
  // API is configured; the API itself is the gate (a non-admin session gets a 403,
  // surfaced as "not authorized"). So we never need to trust a client-side role.
  const navItems = [
    { label: "Ask", icon: "💬", href: "#", current: true, onSelect: () => selectMode("ask") },
    { label: "Panel", icon: "▤", href: "#", onSelect: () => selectMode("panel") },
    { label: "Analyze", icon: "📊", href: "#", onSelect: () => selectMode("analyze") },
  ];
  if (config.adminUrl) {
    navItems.push({ label: "Admin · Usage", icon: "🛠", href: "#", onSelect: () => void screens.showAdmin() });
  }
  // Natural-language drafting (#118c). The endpoint clamps any draft to the author's
  // verified authority server-side; this screen just describes → renders the bounded plan.
  if (config.draftingUrl) {
    navItems.push({ label: "Draft an agent", icon: "✎", href: "#", onSelect: () => screens.showDraft() });
  }
  // Graphical authoring (#117). The bounded menu is pre-clamped to the author's reach
  // server-side; the assembled spec funnels through the same compiler clamp as a draft.
  if (config.authoringUrl) {
    navItems.push({ label: "Build an agent", icon: "🧩", href: "#", onSelect: () => void screens.showBuild() });
  }
  // Collaborative rooms (#116). The room's reach is the server-enforced intersection of its
  // members; every message is attributed + budget-gated. Polling transport ($0-idle).
  if (config.roomsUrl) {
    navItems.push({ label: "Rooms", icon: "👥", href: "#", onSelect: () => void screens.showRoom() });
  }
  // Corpus (#191). Upload + browse the user's own in-scope documents; the endpoint
  // fences every read/write to the verified tenant/scope. Gated on VITE_CORPUS_URL.
  if (config.corpusUrl) {
    navItems.push({ label: "Documents", icon: "📄", href: "#", onSelect: () => screens.showCorpus() });
  }
  const { topbar } = mountChrome({
    brand: "agate",
    tag: "GenAI gateway",
    actions: [whoami, authBtn],
    items: navItems,
  });
  app.insertBefore(topbar, app.firstChild);

  function selectMode(value: string): void {
    screens.cancelPolling(); // leaving the room view stops its poll loop
    const sel = document.getElementById("mode") as HTMLSelectElement | null;
    if (sel) sel.value = value;
    (document.getElementById("q") as HTMLInputElement | null)?.focus();
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
  // Show who's signed in (from the id_token) beside the auth button.
  const who = loggedIn ? identityFromToken(idpToken()) : "";
  if (who) {
    whoami.textContent = `Signed in as ${who}`;
    whoami.title = who;
    whoami.hidden = false;
  }
  if (!loggedIn) {
    scopeEl.textContent = loginConfig
      ? "Log in to start — you'll get a session scoped to your entitlements."
      : "No token: append #idp_token=<jwt> to the URL, or wire VITE_COGNITO_DOMAIN for a login button.";
    form.querySelectorAll("input,select,button").forEach((el) => ((el as HTMLInputElement).disabled = true));
    return;
  }

  // `creds` + `screens` were created early (before the nav). Now that the user is logged in,
  // create the pop-out feature clients (drafting/deploy/corpus/authoring/rooms).
  screens.initClients();
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

  const out = document.getElementById("out")!;
  const mainCol = document.getElementById("main")!;
  // One chat-anchored scroll model over the main column, shared by BOTH renderers (chat transcript
  // + cell view) so scrolling feels identical whichever costume a turn wears (Canvas #242). The
  // "↓ new" pill lets a scrolled-up reader jump back to the newest content.
  const scroll = new ScrollAnchor(mainCol);
  scroll.mountPill();
  const input = document.getElementById("q") as HTMLTextAreaElement;
  const modeSel = document.getElementById("mode") as HTMLSelectElement;
  const modelSel = document.getElementById("model") as HTMLSelectElement;
  const emptyState = document.getElementById("empty");

  // Agent-cell mode (#248) only works when the agent runtime is deployed. Remove the option (and
  // its optgroup) when it isn't, so a user never picks it, does the work, and hits a dead end at
  // launch (#248 UX review). Panel/Analyze also need `agent`, but those pre-date this and route
  // differently; gating them is a separate cleanup.
  if (!agent) {
    modeSel.querySelector('option[value="agent"]')?.closest("optgroup, option")?.remove();
  }

  // Empty-chat presentation: show the empty-state hint AND centre the composer as a landing state
  // (a fresh chat has no transcript above the composer, so top-aligning it just leaves dead space).
  // One source of truth for both, since three call sites toggle emptiness. `empty` = no turns and
  // not in the cell view.
  const syncEmptyState = (empty: boolean): void => {
    if (emptyState) emptyState.hidden = !empty;
    mainCol.classList.toggle("landing", empty);
  };

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
    // contextTokens now reflects what's actually SENT next turn (after clear/window/summary),
    // which may be less than the full transcript.
    ctxText.textContent = chat.contextTokens
      ? `${chat.contextTokens.toLocaleString()} / ${windowTokens.toLocaleString()} tokens sent · ${Math.round(pct)}% · ${chat.turns} turn${chat.turns === 1 ? "" : "s"}`
      : chat.turns > 0
        ? "0 tokens sent · context cleared"
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
    anchor: scroll,
    listHost: document.getElementById("chat-list")!,
    transport: askTransport,
    contextProvider: groundingProvider,
    confirmDelete: (title) => window.confirm(`Delete “${title}”? This can't be undone.`),
    // "Run this" (#243) from a streamed chat answer: the answer is still a plain chat turn (no cell
    // id yet). runCodeFromAnswer levels up (projecting history into cells), then we map the answering
    // turn's ordinal to the matching projected cell (the turnIndex-th answered cell, in order) so the
    // code lands directly below THAT turn — not at the bottom.
    onRunCode: (code, turnIndex) =>
      void runCodeFromAnswer((nb) => {
        // Match ChatTranscript's ordinal predicate exactly (a non-empty answer). An empty-string
        // answer is pushed to history but never counted there, so filtering on answer!==undefined
        // here would drift the index by one; use the same trim() test to stay aligned.
        const answered = nb.cells.filter((c: NotebookCell) => c.kind === "prompt" && !!c.answer?.trim());
        return answered[turnIndex]?.id;
      }, code),
    onActiveChange: (chat) => {
      renderContext(chat, contextWindowFor(chat.modelId));
      syncEmptyState(chat.turns === 0 && chat.view === "chat");
      // Switching chats swaps the visible pane — re-anchor at the new chat's bottom so a "↓ New"
      // pill from the previous chat can't linger and the reader isn't left at a stale scroll pos.
      scroll.reset();
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
    // A fresh chat must read as "a chat app" — never inherit a leftover Code mode from the
    // previous chat (the toggle is sticky only across a single composer, reset on submit).
    resetComposerToAsk();
    input.focus();
  });

  // Context controls (#2): clear / sliding window / compress. All operate on the active
  // chat's send-policy — the transcript is untouched; only what's sent to the model changes.
  const ctxWindow = document.getElementById("ctx-window") as HTMLSelectElement | null;
  const ctxClear = document.getElementById("ctx-clear");
  const ctxCompress = document.getElementById("ctx-compress") as HTMLButtonElement | null;
  const ctxNote = document.getElementById("ctx-note");
  const showCtxNote = (msg: string): void => {
    if (!ctxNote) return;
    ctxNote.textContent = msg;
    ctxNote.hidden = false;
  };
  ctxWindow?.addEventListener("change", () => {
    const n = Number(ctxWindow.value);
    chats.setWindow(n > 0 ? n : undefined);
    showCtxNote(n > 0 ? `Sending only the last ${n} turns.` : "Sending all turns.");
  });
  ctxClear?.addEventListener("click", () => {
    chats.clearContext();
    if (ctxWindow) ctxWindow.value = "0";
    showCtxNote("Context cleared — the next turn starts fresh (transcript kept).");
  });
  ctxCompress?.addEventListener("click", async () => {
    // Summarize the whole conversation body up to now, then send [summary + later turns].
    const body = chats.current.history.filter((m) => m.role !== "system");
    if (body.length < 2) {
      showCtxNote("Nothing to compress yet.");
      return;
    }
    ctxCompress.disabled = true;
    ctxCompress.textContent = "Compressing…";
    try {
      const transcript = body.map((m) => `${m.role}: ${m.content}`).join("\n");
      const model = modelSel?.value && modelSel.value !== AUTO ? modelSel.value : config.defaultModelId;
      let summary = "";
      let cost: number | undefined;
      let budget: BudgetStatus | undefined;
      for await (const chunk of askTransport.converse({
        modelId: model,
        maxTokens: 512,
        messages: [
          {
            role: "user",
            content:
              `Summarize the following conversation compactly, preserving key facts, ` +
              `decisions, and open questions so it can stand in for the earlier turns. ` +
              `Use terse bullet points.\n\n${transcript.slice(0, 8000)}`,
          },
        ],
      })) {
        if (chunk.delta) summary += chunk.delta;
        if (chunk.cost !== undefined) cost = chunk.cost;
        if (chunk.budget) budget = chunk.budget;
      }
      if (summary.trim()) {
        chats.applySummary(summary.trim());
        meter.record(cost, budget);
        showCtxNote("Compressed earlier turns into a summary (transcript kept).");
      } else {
        showCtxNote("Compression produced no summary — context unchanged.");
      }
    } catch (err) {
      showCtxNote(`Compression failed: ${(err as Error).message}`);
    } finally {
      ctxCompress.disabled = false;
      ctxCompress.textContent = "Compress";
    }
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

  // Chips only make sense for Ask; hide them in Panel/Analyze/pattern modes and in Code. The
  // placeholder verb also tracks the composer mode ("Ask a question…" / "Write Python…" / …).
  const PLACEHOLDERS: Record<string, string> = {
    ask: "Ask a question…",
    panel: "Pose a question for the panel…",
    analyze: "Describe an analysis to run…",
    agent: "Research question — a capped background agent (set its budget next)…",
  };
  // Ask · Code switch (Ask-weighted): Ask (default) sends a billed question; Code sends Python
  // that runs locally in the pyodide worker (free). Reaching for Code is what grows the document
  // a spine — the first Code submit levels the surface up to the cell view.
  let codeMode = false;
  const codeToggle = document.getElementById("code-toggle") as HTMLButtonElement | null;
  const sendBtn = form.querySelector(".send-btn") as HTMLButtonElement | null;
  const syncComposerMode = (): void => {
    const mode = modeSel.value;
    codeToggle?.setAttribute("aria-pressed", String(codeMode));
    codeToggle?.classList.toggle("active", codeMode);
    input.placeholder = codeMode
      ? "Write Python — runs locally in your browser…"
      : mode.startsWith("pattern:")
        ? "Pose a question for this reasoning pattern…"
        : (PLACEHOLDERS[mode] ?? "Ask a question…");
    // The mode must be unambiguous per submission even after the placeholder is gone (spec):
    // Code mode monospaces the field and relabels it for screen readers, and the send button
    // becomes a Run glyph. Toggling refocuses the textarea, so its new label is announced.
    input.classList.toggle("code-input", codeMode);
    input.setAttribute("aria-label", codeMode ? "Python code — runs locally in your browser" : "Your question");
    if (sendBtn) {
      sendBtn.innerHTML = codeMode ? "&#x25B6;" : "&#x2191;";
      const verb = codeMode ? "Run" : "Send";
      sendBtn.setAttribute("aria-label", verb);
      sendBtn.title = verb;
    }
    // Suggestion chips are for Ask only: hidden in Code, in the cell view, and in Panel/Analyze.
    if (chipsHost) chipsHost.hidden = codeMode || chats.current.view === "notebook" || mode !== "ask";
  };
  codeToggle?.addEventListener("click", () => {
    codeMode = !codeMode;
    syncComposerMode();
    input.focus();
  });
  const resetComposerToAsk = (): void => {
    if (!codeMode) return;
    codeMode = false;
    syncComposerMode();
  };
  modeSel.addEventListener("change", syncComposerMode);
  syncComposerMode();

  // --- Notebook view (#185) -------------------------------------------------
  // A view of the current chat: the transcript projected into editable prompt cells, each
  // re-runnable as a STANDALONE metered call (not a ChatSession turn, so it never pollutes
  // the transcript). The context gauge stays chat-scoped (cell runs don't touch history).
  const resolvePin = (): string => (modelSel.value === AUTO ? AUTO : modelSel.value);
  // Snapshot of an expanded prompt cell's pre-edit state (prompt + stale), so collapsing without a
  // re-run reverts exactly. Cleared on a successful re-run (the new answer is what we keep).
  const preEditState = new Map<string, { prompt: string; stale: boolean }>();
  // Editing a cell's source keeps it in sync and stale-marks any dependents (#200 slice 3), so a
  // downstream cell that references {{cN}} shows "stale — re-run" once cN's source changes. We
  // only repaint when the stale set actually changes, so ordinary typing doesn't disturb the caret.
  const onNotebookEdit = (cellId: string, source: string): void => {
    const nb = chats.notebookFor(chats.current);
    const cell = nb.cells.find((c: NotebookCell) => c.id === cellId);
    if (!cell) return;
    cell.prompt = source;
    // Editing an answered cell away from the prompt that produced its answer makes IT stale (not
    // just its dependents): the frozen answer no longer corresponds to the question, so we must
    // never re-present it as an authoritative chat turn. We only SET stale here (never clear it —
    // clearing happens on an explicit Run or a Cancel/revert), so an upstream-triggered stale flag
    // isn't lost. (Data-integrity fix — the displayed answer/question must always match.)
    const wasStale = cell.stale ?? false;
    if (isEditedSinceRun(cell)) cell.stale = true;
    const selfStaleChanged = (cell.stale ?? false) !== wasStale;
    const deps = dependentsOf(nb.cells, cellId);
    const newlyStale = deps.filter((d) => !d.stale);
    if (newlyStale.length || selfStaleChanged) {
      for (const d of newlyStale) d.stale = true;
      paintNotebook();
    }
  };
  const paintNotebook = (): void => {
    const chat = chats.current;
    const nb = chats.notebookFor(chat);
    // Preserve the focused editor + caret across a repaint (stale-marking repaints mid-typing).
    const active = document.activeElement as HTMLTextAreaElement | null;
    const focusId = active?.id?.startsWith("nb-") ? active.id : null;
    const caret = focusId ? active!.selectionStart : null;
    renderNotebook(nb, chat.notebookEl, {
      onRun: (cellId, prompt) => void runNotebookCell(cellId, prompt),
      onRunCode: (cellId, code) => void runNotebookCode(cellId, code),
      onRunAgent: (cellId, prompt, cap) => void runNotebookAgent(cellId, prompt, cap),
      onCancelAgent: (cellId) => agentAborters.get(cellId)?.abort(),
      onEdit: (cellId, source) => onNotebookEdit(cellId, source),
      // Two-renderer costume change (#242): "Edit" grows a chat turn into its editable cell;
      // collapsing (Cancel/Done) DISCARDS unrun edits by reverting the cell to EXACTLY its
      // pre-edit state (prompt + stale flag snapshotted on expand). So an accidental edit is freely
      // reversible and can never strand a mismatched Q&A, while a genuine upstream-stale flag isn't
      // wrongly cleared. (A real re-run collapses via runPromptCore instead, keeping the new
      // prompt+answer.) Focus the editor on expand so typing is immediate.
      onToggleExpand: (cellId, expanded) => {
        const cell = nb.cells.find((c: NotebookCell) => c.id === cellId);
        if (!cell) return;
        if (expanded) {
          preEditState.set(cellId, { prompt: cell.prompt, stale: cell.stale ?? false });
        } else {
          const snap = preEditState.get(cellId);
          if (snap) {
            cell.prompt = snap.prompt; // discard unrun edits
            cell.stale = snap.stale; // restore the exact pre-edit staleness
            preEditState.delete(cellId);
          }
        }
        cell.expanded = expanded;
        paintNotebook();
        if (expanded) {
          chat.notebookEl
            .querySelector<HTMLTextAreaElement>(`[data-cell-id="${cellId}"] .notebook-cell-prompt`)
            ?.focus();
        }
      },
      // "Run this" (#243): a python block in a cell's answer spawns a code cell below that cell.
      onRunFromAnswer: (afterCellId, code) => void runCodeFromAnswer(() => afterCellId, code),
      // Per-cell model pin (#247/#237): set/clear a cell's own model. Options are the session's
      // ENTITLED models (minus "Auto" — the cell's "default" option already means follow-composer).
      onSetModel: (cellId, modelId) => {
        const cell = nb.cells.find((c: NotebookCell) => c.id === cellId);
        if (!cell) return;
        // Changing the model of an ANSWERED cell makes it stale: the model is an input to the
        // frozen answer (meta.modelId records which model produced it), so a different model would
        // yield a different answer — flag it (↻ Refresh) rather than silently disagree (#247).
        if (cell.answer && modelId !== cell.modelId) cell.stale = true;
        cell.modelId = modelId;
        paintNotebook();
      },
      // Agent-cell cap change (#248): the cap is an input to the frozen answer (a bigger cap could
      // find more), so changing it stales an answered cell — an explicit, billed re-run, never a
      // silent re-launch. Repaint so the Run button's enabled state (needs ≥1 cap axis) updates.
      onSetCap: (cellId, cap) => {
        const cell = nb.cells.find((c: NotebookCell) => c.id === cellId);
        if (!cell) return;
        const changed = JSON.stringify(cap) !== JSON.stringify(cell.cap ?? {});
        if (cell.answer && changed) cell.stale = true;
        cell.cap = cap;
        paintNotebook();
      },
      modelOptions: creds.scope?.tier
        ? modelOptions(creds.scope.tier).filter((o) => o.value !== AUTO)
        : undefined,
      // Save/Open only when the corpus endpoint is configured (persistence available).
      onSave: screens.corpusClient() ? () => void saveNotebook() : undefined,
      onOpen: screens.corpusClient() ? () => void openNotebook() : undefined,
    });
    if (focusId) {
      const again = document.getElementById(focusId) as HTMLTextAreaElement | null;
      if (again) {
        again.focus();
        if (caret !== null) again.setSelectionRange(caret, caret);
      }
    }
    // Keep the column pinned to the newest cell as it streams/repaints — unless the reader
    // scrolled up (the shared anchor no-ops then and shows the "↓ new" pill instead).
    scroll.maybeScroll();
  };

  // Notebook save/open (#200 slice 4). A notebook persists as JSON under the corpus
  // `_notebooks/` fence, keyed by a stable id per chat (so re-saving overwrites in place).
  const notebookSaveId = new Map<number, string>();
  const notebookName = new Map<number, string>();
  const newSessionLikeId = (): string => {
    const c = globalThis.crypto;
    return c && "randomUUID" in c ? c.randomUUID() : `nb-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
  };
  const setNotebookStatus = (msg: string): void => {
    const bar = chats.current.notebookEl.querySelector(".notebook-toolbar");
    if (!bar) return;
    let status = bar.querySelector<HTMLElement>(".notebook-status");
    if (!status) {
      status = document.createElement("span");
      status.className = "notebook-status";
      bar.appendChild(status);
    }
    status.textContent = msg;
  };
  const saveNotebook = async (): Promise<void> => {
    const corpus = screens.corpusClient();
    if (!corpus) return;
    const chat = chats.current;
    const nb = chats.notebookFor(chat);
    const defaultName = notebookName.get(chat.id) ?? chat.title ?? "Untitled notebook";
    const name = window.prompt("Save notebook as:", defaultName);
    if (name === null) return; // cancelled
    let id = notebookSaveId.get(chat.id);
    if (!id) {
      id = newSessionLikeId();
      notebookSaveId.set(chat.id, id);
    }
    notebookName.set(chat.id, name);
    // savedAt is stamped here (main loop), not in the pure serializer.
    const stored = serializeNotebook(nb, name, new Date().toISOString());
    const res = await corpus.saveNotebook(id, stored);
    setNotebookStatus(res.ok ? `Saved “${name}”.` : `Save failed: ${res.reason}`);
  };
  const openNotebook = async (): Promise<void> => {
    const corpus = screens.corpusClient();
    if (!corpus) return;
    const listed = await corpus.listNotebooks();
    if (!listed.ok) {
      setNotebookStatus(`Couldn't list notebooks: ${listed.reason}`);
      return;
    }
    if (!listed.notebooks.length) {
      setNotebookStatus("No saved notebooks yet.");
      return;
    }
    // Minimal picker: a numbered prompt (kept dependency-free; a richer modal can come later).
    const lines = listed.notebooks.map((n, i) => `${i + 1}. ${n.id}${n.modified ? `  (${n.modified.slice(0, 10)})` : ""}`);
    const pick = window.prompt(`Open which notebook?\n${lines.join("\n")}\n\nEnter a number:`);
    if (pick === null) return;
    const idx = Number(pick) - 1;
    const chosen = listed.notebooks[idx];
    if (!chosen) {
      setNotebookStatus("No notebook at that number.");
      return;
    }
    const loaded = await corpus.loadNotebook(chosen.id);
    if (!loaded.ok) {
      setNotebookStatus(`Open failed: ${loaded.reason}`);
      return;
    }
    const parsed = deserializeNotebook(loaded.notebook);
    if (!parsed) {
      setNotebookStatus("That file isn't a readable notebook.");
      return;
    }
    // Drop any per-cell model pin the current session isn't entitled to (a Canvas saved by a
    // higher-tier user), so the picker and the data model agree and Run can't send an unentitled
    // id (#247 review). The cell falls back to the composer's Model.
    const entitledNow = creds.scope?.tier ? entitledModels(creds.scope.tier) : [];
    for (const c of parsed.notebook.cells) {
      if (c.modelId && !entitledNow.includes(c.modelId)) c.modelId = undefined;
    }
    // Load into a fresh chat so it doesn't clobber the current conversation.
    const target = chats.newChat();
    target.notebook = parsed.notebook;
    notebookSaveId.set(target.id, chosen.id);
    notebookName.set(target.id, parsed.name);
    chats.setView(target.id, "notebook");
    setView("notebook");
    setNotebookStatus(`Opened “${parsed.name}”.`);
  };

  let codeKernel: CodeKernel | null = null;
  const ensureKernel = (nb: { cells: NotebookCell[] }): CodeKernel => {
    // Lazily spawn the pyodide worker on the FIRST code run (its ~10 MB runtime stays out of
    // the base bundle). No server, no network from the cell (NO CLOCKS; no new surface).
    if (!codeKernel) {
      codeKernel = new CodeKernel({
        onStatus: (status, detail) => {
          if (status !== "ready" && detail) {
            for (const c of nb.cells) if (c.kind === "code" && c.state === "running") c.error = detail;
            paintNotebook();
          }
        },
      });
    }
    return codeKernel;
  };

  // Run ONE prompt cell (no cascade). Resolves {{cN}} references against the notebook first
  // (#200 slice 3) so an AI cell can build on another cell's output. Returns true on success.
  const runPromptCore = async (cell: NotebookCell, nb: { cells: NotebookCell[] }): Promise<boolean> => {
    // Resolve {{cN}} refs to text AND collect any referenced code cell's figures (result→prompt
    // loop, #244), so a prompt can reason over a plot on a multimodal model.
    const { resolved, images } = resolveSourceWithImages(cell, nb.cells);
    if (!resolved.trim()) return false;
    // A cell's own model pin wins (#247) — but CLAMP it to the session's entitled set (fail-closed,
    // same guard as the composer): a pin persisted by a higher-tier user must never send an
    // unentitled model. Falls back to the composer's Model (Auto by default) if the pin isn't
    // entitled or isn't set.
    const entitled = creds.scope?.tier ? entitledModels(creds.scope.tier) : [];
    const model = resolveModelPin(cell.modelId, entitled) ?? resolvePin();
    cell.state = "running";
    cell.error = undefined;
    paintNotebook();
    try {
      const result = await runCell(askTransport, model, resolved, groundingProvider, undefined, images);
      cell.answer = result.text;
      cell.sources = lastSources.slice();
      cell.meta = {
        usage: result.usage,
        cost: result.cost,
        budget: result.budget,
        modelId: result.model ?? (model === AUTO ? undefined : model),
        modelReason: result.modelRoute?.reason,
      };
      cell.state = "idle";
      cell.stale = false;
      cell.expanded = false; // a fresh answer collapses back to the read-only chat costume
      cell.answeredPrompt = cell.prompt; // this answer corresponds to this exact prompt
      meter.record(result.cost, result.budget);
      return true;
    } catch (err) {
      cell.state = "error";
      cell.error = (err as Error).message;
      return false;
    }
  };

  // Run ONE code cell (no cascade). Resolves {{cN}} references (JSON-encoded for code) first.
  const runCodeCore = async (cell: NotebookCell, nb: { cells: NotebookCell[] }): Promise<boolean> => {
    const { resolved } = resolveSource(cell, nb.cells);
    if (!resolved.trim()) return false;
    cell.state = "running";
    cell.output = undefined;
    cell.error = "Running…";
    const kernel = ensureKernel(nb);
    paintNotebook();
    try {
      const out = await kernel.run(resolved);
      cell.output = out;
      cell.state = out.error ? "error" : "idle";
      cell.error = undefined;
      cell.stale = false;
      return !out.error;
    } catch (err) {
      cell.state = "error";
      cell.output = { stdout: "", stderr: "", error: (err as Error).message };
      cell.error = undefined;
      return false;
    }
  };

  // Per-cell AbortControllers for in-flight agent runs, so a user can Cancel a billed background
  // run (aborts the client-side wait; the server run still self-bounds by its cap).
  const agentAborters = new Map<string, AbortController>();

  // Run ONE agent cell (#248, Canvas move #5): a budget/time/step-capped background research run on
  // AgentCore. Resolves {{cN}} refs first (an agent cell can build on prior output). Never auto-run
  // (it spends real money) — only from an explicit launch. Requires the agent transport + a token.
  const runAgentCore = async (cell: NotebookCell, nb: { cells: NotebookCell[] }): Promise<boolean> => {
    const { resolved } = resolveSource(cell, nb.cells);
    if (!resolved.trim()) return false;
    if (!agent) {
      cell.state = "error";
      cell.error = "the agent runtime isn't configured for this deployment";
      return false;
    }
    cell.state = "running";
    cell.error = undefined;
    cell.answer = undefined; // clear any prior (stale) answer while this run streams
    const abort = new AbortController();
    agentAborters.set(cell.id, abort);
    // Live elapsed readout against the time cap — no black-box spinner for a billed background run
    // (#248 UX review). A 1s ticker updates the cell's transient liveProgress; cleared on settle.
    const startedAt = Date.now();
    const capMin = cell.cap?.seconds ? cell.cap.seconds / 60 : undefined;
    const tick = () => {
      const mins = (Date.now() - startedAt) / 60000;
      cell.liveProgress = capMin
        ? `researching · ${mins.toFixed(1)} / ${capMin.toFixed(1)} min`
        : `researching · ${mins.toFixed(1)} min`;
      paintNotebook();
    };
    tick();
    const ticker = window.setInterval(tick, 1000);
    try {
      const { text, receipt } = await runAgentCell(
        agent,
        idpToken(),
        resolved,
        cell.cap ?? {},
        // Stream answer text live into the cell so the reader sees progress, not just dots.
        (delta) => {
          cell.answer = (cell.answer ?? "") + delta;
          paintNotebook();
        },
        (note) => {
          cell.liveProgress = note;
          paintNotebook();
        },
        abort.signal,
      );
      cell.answer = text;
      cell.agentReceipt = receipt;
      cell.state = "idle";
      cell.stale = false;
      cell.liveProgress = undefined;
      cell.answeredPrompt = cell.prompt; // the answer corresponds to this exact question + cap
      // An agent cell spends real money — fold its spend into the session meter like a prompt cell.
      meter.record(receipt.spentUsd, undefined);
      return true;
    } catch (err) {
      // A user Cancel aborts the client-side wait; the cell keeps whatever partial text streamed in
      // and is marked stale (re-run to finish). Any other error surfaces as an error state.
      if (abort.signal.aborted) {
        cell.state = "idle";
        cell.stale = true;
        cell.liveProgress = undefined;
        cell.error = undefined;
        return false;
      }
      cell.state = "error";
      cell.error = (err as Error).message;
      cell.liveProgress = undefined;
      return false;
    } finally {
      window.clearInterval(ticker);
      agentAborters.delete(cell.id);
    }
  };

  // After a cell's value changes, propagate to dependents (#200 slice 3): code dependents
  // re-run automatically in topological order (free, local WASM); prompt (AI) and agent dependents
  // are only marked stale — an explicit, billed re-run — so reactivity never spends money silently.
  const cascadeFrom = async (cellId: string, nb: { cells: NotebookCell[] }): Promise<void> => {
    for (const dep of dependentsOf(nb.cells, cellId)) {
      if (dep.kind === "code") {
        await runCodeCore(dep, nb);
      } else {
        dep.stale = true; // prompt + agent cells never auto-run (they cost money)
      }
    }
  };

  const runNotebookCell = async (cellId: string, prompt: string): Promise<void> => {
    const nb = chats.notebookFor(chats.current);
    const cell = nb.cells.find((c: NotebookCell) => c.id === cellId);
    if (!cell || cell.kind !== "prompt" || !prompt.trim()) return;
    cell.prompt = prompt;
    preEditState.delete(cellId); // a real re-run commits the edit; nothing to revert to
    const ok = await runPromptCore(cell, nb);
    if (ok) await cascadeFrom(cell.id, nb);
    paintNotebook();
  };

  const runNotebookCode = async (cellId: string, code: string): Promise<void> => {
    const nb = chats.notebookFor(chats.current);
    const cell = nb.cells.find((c: NotebookCell) => c.id === cellId);
    if (!cell || cell.kind !== "code" || !code.trim()) return;
    cell.prompt = code;
    const ok = await runCodeCore(cell, nb);
    if (ok) await cascadeFrom(cell.id, nb);
    paintNotebook();
  };

  const runNotebookAgent = async (cellId: string, prompt: string, cap: AgentCap): Promise<void> => {
    const nb = chats.notebookFor(chats.current);
    const cell = nb.cells.find((c: NotebookCell) => c.id === cellId);
    if (!cell || cell.kind !== "agent" || !prompt.trim()) return;
    cell.prompt = prompt;
    cell.cap = cap;
    preEditState.delete(cellId);
    const ok = await runAgentCore(cell, nb);
    if (ok) await cascadeFrom(cell.id, nb);
    paintNotebook();
  };
  // Switch which projection of the active chat is showing. There is no user-facing toggle
  // anymore (Canvas #242): the surface flips to the cell view when the document grows a spine
  // (the composer's Code affordance, or opening a saved notebook). The composer is the
  // in-progress tail cell and stays visible in BOTH views — it is never hidden.
  const setView = (view: "chat" | "notebook"): void => {
    chats.setView(chats.current.id, view);
    if (chipsHost) chipsHost.hidden = view === "notebook" || codeMode || modeSel.value !== "ask";
    syncEmptyState(view === "chat" && chats.current.turns === 0);
    if (view === "notebook") paintNotebook();
    scroll.reset(); // flipping the view swaps the content wholesale — re-anchor at its bottom
  };

  // A sensible default cap for a newly-authored agent cell (#248): a small dollar + time envelope
  // the user then tunes in the cell before launching. Never launched automatically.
  const DEFAULT_AGENT_CAP: AgentCap = { costUsd: 0.5, seconds: 300, maxSteps: 12 };

  // Append a fresh cell of `kind` seeded with `source`, level the surface up to the cell view, and
  // run it. Shared by the composer's Code path (and, once in the cell view, Ask). An "agent" cell
  // is the exception: it is NOT auto-run — it's appended with a default cap for the user to tune,
  // then launched explicitly from its Run button (it spends real money).
  const submitAsCell = async (kind: "prompt" | "code" | "agent", source: string): Promise<void> => {
    const nb = chats.notebookFor(chats.current);
    const cell = newCell(source, kind, nextCellName(nb.cells), kind === "agent" ? DEFAULT_AGENT_CAP : undefined);
    nb.cells.push(cell);
    scroll.onNewTurn(); // a new cell at the bottom resumes anchoring, like a new chat turn
    if (chats.current.view !== "notebook") setView("notebook");
    else paintNotebook();
    if (kind === "code") await runNotebookCode(cell.id, source);
    else if (kind === "agent") {
      // Don't launch — reveal the cell with its cap inputs; the user reviews the envelope + Runs.
      cell.expanded = true;
      paintNotebook();
    } else await runNotebookCell(cell.id, source);
  };

  // "Run this" (#243): a python block in an AI answer spawns a live code cell seeded with that code
  // and runs it — the AI-writes-code bridge. The cell is inserted directly BELOW the answering
  // turn: `resolveAfterId` is called AFTER we level up to the cell view (so history is projected)
  // and returns the id of the cell the code should follow, or undefined to append at the end.
  // Human-in-the-loop: only fires on an explicit Run. Never auto-runs emitted code.
  const runCodeFromAnswer = async (
    resolveAfterId: (nb: { cells: NotebookCell[] }) => string | undefined,
    code: string,
  ): Promise<void> => {
    if (!code.trim()) return;
    // Level the surface up first so the answer is projected into the cell sequence, then insert.
    if (chats.current.view !== "notebook") setView("notebook");
    const nb = chats.notebookFor(chats.current);
    const afterId = resolveAfterId(nb);
    // Dedupe: if this exact code was already Run from this answer, don't spawn a second cell —
    // just scroll to the existing one. (A repaint rebuilds the answer's Run button un-disabled, so
    // the DOM-level guard isn't enough; this state-level check is the real one.)
    const existing = nb.cells.find(
      (c: NotebookCell) => c.kind === "code" && c.spawnedFrom === afterId && c.prompt === code,
    );
    if (afterId && existing) {
      paintNotebook();
      chats.current.notebookEl
        .querySelector(`[data-cell-id="${existing.id}"]`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    const cell = newCell(code, "code", nextCellName(nb.cells));
    // Insert after the answering cell, but AFTER any code cells already spawned from the same answer
    // (so multiple blocks run top-to-bottom keep their order rather than stacking in reverse).
    let at = afterId ? nb.cells.findIndex((c: NotebookCell) => c.id === afterId) : -1;
    if (at >= 0) {
      while (at + 1 < nb.cells.length && nb.cells[at + 1].kind === "code" && nb.cells[at + 1].spawnedFrom === afterId) {
        at++;
      }
      cell.spawnedFrom = afterId;
      nb.cells.splice(at + 1, 0, cell);
    } else {
      nb.cells.push(cell);
    }
    scroll.onNewTurn();
    paintNotebook();
    await runNotebookCode(cell.id, code);
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    autoGrow();
    syncEmptyState(false); // a submission always leaves the landing state

    // Code submission: a local Python cell (free). It levels the surface up to the cell view and
    // resets the composer back to Ask afterwards, so the next typed line isn't sent as Python
    // (the sticky-mode day-one bug). Handled before the Ask path — mode/pattern don't apply.
    if (codeMode) {
      resetComposerToAsk();
      await submitAsCell("code", q);
      input.focus();
      return;
    }
    // Agent mode (#248): append a capped-research agent cell (NOT auto-run — it's revealed with a
    // default cap the user tunes, then launches explicitly). Handled before the notebook-view Ask
    // path so selecting Agent always creates an agent cell, in either view. Mode stays sticky
    // (parity with Panel/Analyze, which share this dropdown) — unlike the transient Code toggle;
    // since an agent cell never auto-runs, a stray Enter appends an idle, review-first cell, not a
    // billed run, so stickiness here can't spend money by surprise.
    if (modeSel.value === "agent") {
      await submitAsCell("agent", q);
      input.focus();
      return;
    }
    // Once the document has a spine (cell view), an Ask appends a prompt cell so the sequence
    // stays one coherent document rather than jumping back to a separate transcript.
    if (chats.current.view === "notebook") {
      await submitAsCell("prompt", q);
      input.focus();
      return;
    }

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
          // Ground the suggestions in the SAME corpus excerpts that grounded the answer,
          // so we don't propose questions the retrieval-grounded assistant will then
          // refuse. Empty when RAG returned nothing → falls back to the open-ended prompt.
          const corpusContext = lastSources.map((c) => c.text).join("\n\n");
          void suggestFollowups(askTransport, m, question, answer, corpusContext).then((r) => {
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
    // Record the turn UNCONDITIONALLY (even an empty answer): ChatSession.send always pushed an
    // assistant message to history, so turnMeta must get one entry per turn too or the notebook
    // projection's per-cell receipts (#245) desync — a later cell would show an earlier turn's
    // cost. recordTurn normalizes missing meta. Follow-ups/memory still require real answer text.
    chats.recordTurn(q, result.text, {
      usage: result.usage,
      cost: result.cost,
      budget: result.budget,
      modelId: ranModel,
      modelReason: result.modelRoute?.reason,
    });
    if (result.text.trim()) {
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
