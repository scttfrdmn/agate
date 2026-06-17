// Natural-language drafting client + view (#118c, the SPA surface for #118b).
//
// "The LLM proposes, the compiler disposes." The user describes an agent in plain
// language; the drafting Function URL asks the author's own entitled model to draft a
// spec, then `agate.drafting.dispose_draft` CLAMPS it to what the author verifiably holds
// and returns the bounded plan. This client POSTs {idp_token, request} and renders the
// plan; the boundary is enforced entirely server-side — the model's draft has ZERO
// authority, so an over-broad request comes back clamped or rejected, never widened.
//
// The Function URL is AWS_IAM-authed, so the POST is SigV4-signed (service "lambda") with
// the broker-vended scoped credentials — same identity boundary as the chokepoint/openai
// path. The idp_token in the body is what the endpoint verifies to derive tenant/scope;
// the SigV4 creds only authorize invoking the endpoint.

import { Sha256 } from "@aws-crypto/sha256-js";
import { SignatureV4 } from "@smithy/signature-v4";

import type { ScopedCredentials } from "../auth";
import { toSdkCredentials as sdkCreds } from "../auth/sdkCreds";

// The endpoint's response contract (infra/functions/drafting/handler.py): a draft
// OUTCOME, never a credential. `plan` is the legible "this agent will / will not" lines.
export interface DraftPlan {
  ok: boolean;
  reason: string;
  plan: string[];
}

export interface DraftConfig {
  region: string;
  // The drafting Function URL (VITE_DRAFTING_URL). Empty disables the Draft screen.
  endpoint: string;
}

// Pure: map the endpoint's HTTP status + JSON body to a DraftPlan. Exported for testing
// without the network. A non-200 (403 not-entitled, 500) becomes a rejected plan with a
// readable reason rather than a thrown error — the screen renders it inline.
export function responseToPlan(status: number, payload: Record<string, unknown>): DraftPlan {
  if (status === 403) {
    const detail = typeof payload.detail === "string" ? payload.detail : "not entitled";
    return { ok: false, reason: detail, plan: [] };
  }
  if (status !== 200) {
    const err = typeof payload.error === "string" ? payload.error : `error ${status}`;
    return { ok: false, reason: err, plan: [] };
  }
  const plan = Array.isArray(payload.plan)
    ? payload.plan.filter((l): l is string => typeof l === "string")
    : [];
  return {
    ok: payload.ok === true,
    reason: typeof payload.reason === "string" ? payload.reason : "",
    plan,
  };
}

export class DraftClient {
  constructor(
    private readonly cfg: DraftConfig,
    private readonly creds: () => Promise<ScopedCredentials>,
    // The campus IdP token — the endpoint verifies it to derive the author's authority.
    // The drafted scope is clamped to it server-side; this client controls nothing about it.
    private readonly idpToken: () => string,
  ) {}

  async draft(request: string): Promise<DraftPlan> {
    const body = JSON.stringify({ idp_token: this.idpToken(), request });
    const url = new URL(this.cfg.endpoint);

    const signer = new SignatureV4({
      service: "lambda", // a Lambda Function URL (AWS_IAM auth), not API Gateway
      region: this.cfg.region,
      credentials: sdkCreds(await this.creds()),
      sha256: Sha256,
    });
    const signed = await signer.sign({
      method: "POST",
      protocol: url.protocol,
      hostname: url.hostname,
      path: url.pathname,
      headers: { host: url.hostname, "content-type": "application/json" },
      body,
    });

    const resp = await fetch(this.cfg.endpoint, {
      method: "POST",
      headers: signed.headers as Record<string, string>,
      body,
    });
    let payload: Record<string, unknown> = {};
    try {
      payload = (await resp.json()) as Record<string, unknown>;
    } catch {
      payload = {};
    }
    return responseToPlan(resp.status, payload);
  }
}

function el(tag: string, cls = "", text?: string): HTMLElement {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

// Pure: render a disposed DraftPlan into `target`. On ok, the bounded plan lines + a
// confirm button (deploy-on-confirm is deferred — the handler explains it). On reject,
// the reason — the clamp/refusal is the whole point, surfaced plainly, not an error.
// `onConfirm` is invoked when the user accepts the rendered plan.
export function renderDraft(
  plan: DraftPlan,
  target: HTMLElement,
  opts: { onConfirm?: () => void } = {},
): void {
  target.replaceChildren();

  if (!plan.ok) {
    const panel = el("section", "panel");
    panel.setAttribute("aria-label", "Draft not accepted");
    panel.appendChild(el("div", "panel-title", "Draft clamped to your authority"));
    // A rejection is the thesis working: the draft asked for more than the author holds.
    panel.appendChild(
      el("p", "cost-line", plan.reason || "The draft could not be bounded to your entitlements."),
    );
    target.appendChild(panel);
    return;
  }

  const panel = el("section", "panel");
  panel.setAttribute("aria-label", "Proposed agent");
  panel.appendChild(el("div", "panel-title", "This agent — bounded to what you hold"));
  const ul = el("ul");
  ul.style.cssText = "list-style:none;display:flex;flex-direction:column;gap:.3rem;margin:.5rem 0";
  for (const line of plan.plan) {
    const li = el("li", "", line);
    li.style.cssText = "padding:.2rem 0";
    ul.appendChild(li);
  }
  panel.appendChild(ul);
  target.appendChild(panel);

  // Confirm. The plan is reviewed; deploy-on-confirm (the #106 spawn_child executor) is
  // not yet wired, so the button explains that rather than pretending to deploy.
  const actions = el("section", "panel");
  actions.setAttribute("aria-label", "Confirm");
  const note = el("p", "cost-line", "Review the bounds above. Nothing is created until you confirm.");
  actions.appendChild(note);
  const btn = el("button", "btn", "Confirm & create agent") as HTMLButtonElement;
  btn.type = "button";
  btn.onclick = () => {
    if (opts.onConfirm) opts.onConfirm();
    note.textContent = "Confirmed. Deploy-on-confirm is not enabled in this build yet (#118c).";
    btn.disabled = true;
  };
  actions.appendChild(btn);
  target.appendChild(actions);
}
