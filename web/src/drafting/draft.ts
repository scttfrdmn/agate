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
// OUTCOME, never a credential. `plan` is the legible "this agent will / will not" lines;
// `spec` is the validated draft dict the user confirms (echoed to the deploy endpoint, which
// RE-CLAMPS it server-side — so it carries no authority, it's a convenience).
export interface DraftPlan {
  ok: boolean;
  reason: string;
  plan: string[];
  spec?: Record<string, unknown>;
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
  const spec =
    payload.spec && typeof payload.spec === "object" && !Array.isArray(payload.spec)
      ? (payload.spec as Record<string, unknown>)
      : undefined;
  return {
    ok: payload.ok === true,
    reason: typeof payload.reason === "string" ? payload.reason : "",
    plan,
    spec,
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

// The deploy endpoint's response contract (infra/functions/deploy/handler.py): on ok, the
// created agent's id + the persisted plan; otherwise the re-clamp/refusal reason. Never a
// credential — agate persists the governed spec, it does not vend a standing credential.
export interface DeployResult {
  ok: boolean;
  reason: string;
  agentId: string;
  plan: string[];
}

export interface DeployConfig {
  region: string;
  // The deploy Function URL (VITE_DEPLOY_URL). Empty disables the confirm action.
  endpoint: string;
}

// Pure: map the deploy endpoint's status + body to a DeployResult. Exported for testing.
export function responseToDeploy(status: number, payload: Record<string, unknown>): DeployResult {
  if (status === 403) {
    const detail = typeof payload.detail === "string" ? payload.detail : "not entitled";
    return { ok: false, reason: detail, agentId: "", plan: [] };
  }
  if (status !== 200) {
    const err = typeof payload.error === "string" ? payload.error : `error ${status}`;
    return { ok: false, reason: err, agentId: "", plan: [] };
  }
  const plan = Array.isArray(payload.plan)
    ? payload.plan.filter((l): l is string => typeof l === "string")
    : [];
  return {
    ok: payload.ok === true,
    reason: typeof payload.reason === "string" ? payload.reason : "",
    agentId: typeof payload.agent_id === "string" ? payload.agent_id : "",
    plan,
  };
}

export class DeployClient {
  constructor(
    private readonly cfg: DeployConfig,
    private readonly creds: () => Promise<ScopedCredentials>,
    private readonly idpToken: () => string,
  ) {}

  // Confirm-and-create: POST the validated spec; the endpoint RE-CLAMPS it against the
  // verified token server-side and persists the governed record. The spec carries no
  // authority — a tampered one is re-clamped or rejected exactly as a fresh draft.
  async deploy(spec: Record<string, unknown>): Promise<DeployResult> {
    const body = JSON.stringify({ idp_token: this.idpToken(), spec });
    const url = new URL(this.cfg.endpoint);
    const signer = new SignatureV4({
      service: "lambda", // a Lambda Function URL (AWS_IAM auth)
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
    return responseToDeploy(resp.status, payload);
  }
}

function el(tag: string, cls = "", text?: string): HTMLElement {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

// Pure: render a disposed DraftPlan into `target`. On ok, the bounded plan lines + a confirm
// button; on reject, the reason — the clamp/refusal is the whole point, surfaced plainly, not
// an error. `onConfirm` (when supplied) is the async deploy-on-confirm action: it returns a
// DeployResult, which is rendered in place. With no `onConfirm` (or no spec), the button
// explains that deploy isn't wired rather than pretending to create.
export function renderDraft(
  plan: DraftPlan,
  target: HTMLElement,
  opts: { onConfirm?: (spec: Record<string, unknown>) => Promise<DeployResult> } = {},
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

  // Confirm — the deploy-on-confirm action. The server RE-CLAMPS the spec against the verified
  // token before persisting, so the echoed spec carries no authority.
  const actions = el("section", "panel");
  actions.setAttribute("aria-label", "Confirm");
  const note = el("p", "cost-line", "Review the bounds above. Nothing is created until you confirm.");
  actions.appendChild(note);
  const btn = el("button", "btn", "Confirm & create agent") as HTMLButtonElement;
  btn.type = "button";

  const canDeploy = Boolean(opts.onConfirm && plan.spec);
  if (!canDeploy) {
    btn.onclick = () => {
      note.textContent = "Deploy-on-confirm is not enabled in this build.";
      btn.disabled = true;
    };
  } else {
    btn.onclick = async () => {
      btn.disabled = true;
      note.textContent = "Creating…";
      try {
        const result = await opts.onConfirm!(plan.spec!);
        if (result.ok) {
          note.textContent = `Created: ${result.agentId}`;
        } else {
          note.textContent = `Not created: ${result.reason || "rejected"}`;
          btn.disabled = false; // let them retry / re-draft
        }
      } catch (err) {
        note.textContent = `Create failed: ${(err as Error).message}`;
        btn.disabled = false;
      }
    };
  }
  actions.appendChild(btn);
  target.appendChild(actions);
}
