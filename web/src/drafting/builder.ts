// Visual agent builder (#117 PR 2, the SPA surface for the #117 authoring endpoint).
//
// The bounded-menu builder: the server's `options` op returns ONLY the tiers/scopes the author
// holds + the capability/skill/pattern catalogs + the template gallery, so the form literally
// cannot render an over-broad choice (escalation = the absence of the option). The assembled
// spec funnels through the `dispose` op (the SAME compiler clamp an LLM draft uses), and the
// confirm step reuses the #118 deploy flow (renderDraft + DeployClient). The client makes NO
// authority decision — the menu is UX, the compiler is the boundary.
//
// The Function URL is AWS_IAM-authed, so calls are SigV4-signed (service "lambda") with the
// broker-vended scoped creds — same identity boundary as the drafting/openai path.

import { Sha256 } from "@aws-crypto/sha256-js";
import { SignatureV4 } from "@smithy/signature-v4";

import type { ScopedCredentials } from "../auth";
import { toSdkCredentials as sdkCreds } from "../auth/sdkCreds";
import { type DraftPlan, responseToPlan } from "./draft";

// The bounded menu the endpoint returns (mirrors agate.authoring.AuthoringOptions.to_dict).
export interface AuthoringOptions {
  author_tier: string;
  author_scope: string;
  offerable_tiers: string[];
  offerable_scopes: string[];
  capabilities: { name: string; description?: string }[];
  skills: { name: string; description?: string }[];
  patterns: { key: string; description?: string }[];
  budget_per: string[];
  budget_periods: string[];
  trigger_kinds: string[];
}

export interface TemplateRow {
  id: string;
  name: string;
  description: string;
}

export interface OptionsResponse {
  ok: boolean;
  options?: AuthoringOptions;
  templates?: TemplateRow[];
}

export interface AuthoringConfig {
  region: string;
  // The authoring Function URL (VITE_AUTHORING_URL). Empty disables the Build screen.
  endpoint: string;
}

// The builder's form state — what the user selected. Pure input to `buildSpecFromForm`.
export interface BuilderForm {
  agent: string;
  description: string;
  scope: string;
  reasoning?: string;
  tools: string[];
  budget?: string;
}

// Pure: assemble a spec dict from the form state. Mirrors agate.authoring.build_spec — empty
// optional fields are OMITTED so parse_spec's defaults apply. Exported for testing. The role
// is NOT a form field: the server derives tier from the author's token, and the builder never
// offers a role above the author (the menu is pre-clamped), so a fixed "member"-ish role is
// assembled and the compiler clamps tier regardless.
export function buildSpecFromForm(form: BuilderForm): Record<string, unknown> {
  const spec: Record<string, unknown> = {
    agent: form.agent.trim(),
    description: form.description.trim(),
    role: "member",
  };
  if (form.scope) spec.scope = form.scope;
  if (form.reasoning) spec.reasoning = form.reasoning;
  if (form.tools.length) spec.tools = [...form.tools];
  if (form.budget && form.budget.trim()) spec.budget = form.budget.trim();
  return spec;
}

async function signedPost(
  cfg: AuthoringConfig,
  creds: () => Promise<ScopedCredentials>,
  body: string,
): Promise<{ status: number; payload: Record<string, unknown> }> {
  const url = new URL(cfg.endpoint);
  const signer = new SignatureV4({
    service: "lambda", // a Lambda Function URL (AWS_IAM auth)
    region: cfg.region,
    credentials: sdkCreds(await creds()),
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
  const resp = await fetch(cfg.endpoint, {
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
  return { status: resp.status, payload };
}

export class AuthoringClient {
  constructor(
    private readonly cfg: AuthoringConfig,
    private readonly creds: () => Promise<ScopedCredentials>,
    private readonly idpToken: () => string,
  ) {}

  // Fetch the bounded menu + template gallery. The menu is pre-clamped server-side to the
  // author's reach.
  async options(): Promise<OptionsResponse> {
    const body = JSON.stringify({ idp_token: this.idpToken(), op: "options" });
    const { status, payload } = await signedPost(this.cfg, this.creds, body);
    if (status !== 200) return { ok: false };
    const options = payload.options as AuthoringOptions | undefined;
    const templates = Array.isArray(payload.templates)
      ? (payload.templates as TemplateRow[])
      : [];
    return { ok: payload.ok === true, options, templates };
  }

  // Dispose a builder-assembled spec (or template overlay) -> the bounded plan to confirm. The
  // server re-clamps; a hand-crafted over-broad spec comes back ok=false. Returns a DraftPlan
  // so renderDraft (the #118 confirm UI) can render it unchanged.
  async dispose(spec: Record<string, unknown>, template?: string): Promise<DraftPlan> {
    const req: Record<string, unknown> = { idp_token: this.idpToken(), op: "dispose", spec };
    if (template) req.template = template;
    const { status, payload } = await signedPost(this.cfg, this.creds, JSON.stringify(req));
    return responseToPlan(status, payload);
  }
}
