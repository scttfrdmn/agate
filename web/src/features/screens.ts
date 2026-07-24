// Pop-out feature screens (#221) — the nav-triggered surfaces that render into the main #out
// region: Admin, Documents (corpus), Draft an agent, Build an agent, Rooms. Extracted from the
// main.ts orchestration monolith into a controller factory: `createScreens(ctx)` owns the
// post-login feature clients and the room poll-cancellation token, and returns the `show*`
// handlers the nav wires up. main() stays the thin composition root that builds the context,
// registers nav items, and calls `initClients()` after login.
//
// Behaviour is unchanged from the in-main implementation; this is a structural move so an
// experimental screen can't tangle with the core chat/notebook path.

import { config } from "../config";
import type { CredentialManager } from "../auth/credentials";
import { fetchAdmin, renderAdmin } from "../admin/view";
import { type AuthoringOptions, AuthoringClient, type TemplateRow } from "../drafting/builder";
import { buildSpecFromForm } from "../drafting/builder";
import { DeployClient, DraftClient, renderDraft } from "../drafting/draft";
import { RoomClient } from "../rooms/client";
import { renderMembers, renderMessages } from "../rooms/view";
import { CorpusClient } from "../corpus/client";
import { renderCorpus } from "../corpus/view";
import { renderError } from "../app/dom";

// The shared runtime dependencies the screens need from the app shell.
export interface ScreensContext {
  idpToken: () => string;
  creds: CredentialManager;
}

export interface Screens {
  showAdmin: () => Promise<void>;
  showCorpus: () => void;
  showDraft: () => void;
  showBuild: () => Promise<void>;
  showRoom: () => Promise<void>;
  /** Bump the poll token to cancel any running room poll loop (called when nav leaves a view). */
  cancelPolling: () => void;
  /** Create the feature clients that need scoped creds — call once after login. */
  initClients: () => void;
  /** The corpus client (once initialised), so the notebook save/open path can reuse it. */
  corpusClient: () => CorpusClient | null;
}

export function createScreens(ctx: ScreensContext): Screens {
  const { idpToken, creds } = ctx;

  // Feature clients, created after login (they need scoped creds for SigV4). Held in the
  // module closure instead of main()'s.
  let draftClient: DraftClient | null = null;
  let deployClient: DeployClient | null = null;
  let authoringClient: AuthoringClient | null = null;
  let corpusClient: CorpusClient | null = null;
  let roomClient: RoomClient | null = null;

  // A monotonically-bumped token: each screen entry/nav bump cancels any prior poll loop, so
  // navigating away stops polling (no standing connection — NO CLOCKS on the client too).
  let roomPollToken = 0;
  const cancelPolling = (): void => {
    roomPollToken += 1;
  };

  // Render the governed-access console into the main region. Admin-gated server-side.
  async function showAdmin(): Promise<void> {
    const out = document.getElementById("out");
    if (!out) return;
    cancelPolling();
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

  // Corpus screen (#191): upload + browse the user's own in-scope documents. The endpoint
  // fences every read/write to the verified tenant/scope; this view just drives it.
  function showCorpus(): void {
    const outEl = document.getElementById("out");
    if (!outEl) return;
    cancelPolling();
    outEl.replaceChildren();
    if (!corpusClient) {
      renderError(outEl, "Log in to manage documents — the corpus is scoped to your access.");
      return;
    }
    renderCorpus(corpusClient, outEl);
  }

  // Render the natural-language drafting surface (#118c): a textarea to describe an agent, then
  // the server-clamped bounded plan + a confirm step. The boundary is enforced server-side — the
  // model's draft has zero authority.
  function showDraft(): void {
    const outEl = document.getElementById("out");
    if (!outEl) return;
    cancelPolling();
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

  // The visual builder (#117): fetch the bounded menu, render a form whose scope/tier choices are
  // ALREADY clamped to the author (unsafe is unrepresentable), then dispose the assembled spec
  // through the same compiler clamp + the #118 confirm/deploy flow.
  async function showBuild(): Promise<void> {
    const outEl = document.getElementById("out");
    if (!outEl) return;
    cancelPolling();
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
  async function showRoom(): Promise<void> {
    const outEl = document.getElementById("out");
    if (!outEl) return;
    cancelPolling();
    outEl.replaceChildren();
    if (!roomClient) {
      renderError(outEl, "Log in to use rooms — a room runs under your own entitlements.");
      return;
    }
    // A minimal room launcher: open a new room or join one by id, then enter the live view.
    const panel = document.createElement("section");
    panel.className = "panel";
    panel.setAttribute("aria-label", "Rooms");
    panel.appendChild(
      Object.assign(document.createElement("div"), {
        className: "panel-title",
        textContent: "Collaborative rooms",
      }),
    );
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

  // Create the feature clients that need scoped creds — call once after login. Each is gated on
  // its endpoint being configured (the SPA hides a screen whose endpoint is unset).
  function initClients(): void {
    if (config.draftingUrl) {
      draftClient = new DraftClient(
        { region: config.region, endpoint: config.draftingUrl },
        () => creds.get(),
        () => idpToken(),
      );
    }
    if (config.deployUrl) {
      deployClient = new DeployClient(
        { region: config.region, endpoint: config.deployUrl },
        () => creds.get(),
        () => idpToken(),
      );
    }
    if (config.corpusUrl) {
      corpusClient = new CorpusClient(
        { region: config.region, endpoint: config.corpusUrl },
        () => creds.get(),
        () => idpToken(),
      );
    }
    if (config.authoringUrl) {
      authoringClient = new AuthoringClient(
        { region: config.region, endpoint: config.authoringUrl },
        () => creds.get(),
        () => idpToken(),
      );
    }
    if (config.roomsUrl) {
      roomClient = new RoomClient(
        { region: config.region, endpoint: config.roomsUrl },
        () => creds.get(),
        () => idpToken(),
      );
    }
  }

  return {
    showAdmin,
    showCorpus,
    showDraft,
    showBuild,
    showRoom,
    cancelPolling,
    initClients,
    corpusClient: () => corpusClient,
  };
}
