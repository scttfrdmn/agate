// Memory client (#194) — recall/record cross-session memory for the Ask chat.
//
// POSTs {idp_token, op, ...} to the memory Function URL (AWS_IAM auth), SigV4-signed
// (service "lambda") with the broker-vended scoped creds — same identity boundary as the
// corpus/chokepoint paths. The endpoint derives every namespace from the VERIFIED token
// (tenant/scope/subject), never a client field, so this client cannot read or write
// another tenant/principal/scope. Opt-in: only constructed when VITE_MEMORY_URL is set
// (and the billable agate-memory stack deployed). Best-effort — memory never blocks a turn.

import { Sha256 } from "@aws-crypto/sha256-js";
import { SignatureV4 } from "@smithy/signature-v4";

import type { ScopedCredentials } from "../auth";
import { toSdkCredentials as sdkCreds } from "../auth/sdkCreds";

export type MemoryTier = "session" | "personal" | "shared";

export interface MemoryTurn {
  role: "user" | "assistant";
  text: string;
}

export interface MemoryConfig {
  region: string;
  endpoint: string; // VITE_MEMORY_URL; empty disables memory
}

// Pure: pull the readable text out of one recalled record (the shape varies across the
// semantic/summary strategies — try the known keys, mirror agent.memory_client). Exported
// for testing.
export function recordText(rec: Record<string, unknown>): string {
  const raw = rec.content ?? rec.text ?? rec.memoryContent ?? "";
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    const t = (raw as Record<string, unknown>).text;
    return typeof t === "string" ? t.trim() : "";
  }
  return typeof raw === "string" ? raw.trim() : "";
}

// Pure: render recalled records into a grounding text block to prepend to a turn
// (mirrors agent.memory_client.recall_as_evidence). Returns "" when nothing usable.
export function recallToContext(records: Array<Record<string, unknown>>): string {
  const lines = records.map(recordText).filter((t) => t.length > 0);
  if (!lines.length) return "";
  return "Relevant remembered context:\n" + lines.map((l) => `- ${l}`).join("\n");
}

export class MemoryClient {
  constructor(
    private readonly cfg: MemoryConfig,
    private readonly creds: () => Promise<ScopedCredentials>,
    private readonly idpToken: () => string,
  ) {}

  /** Recall records from one tier (default personal — cross-session continuity). Returns
   *  a grounding text block ready to prepend, or "" on any failure (best-effort). */
  async recall(opts: { tier?: MemoryTier; query: string; sessionId: string }): Promise<string> {
    try {
      const { status, body } = await this.post({
        op: "recall",
        tier: opts.tier ?? "personal",
        query: opts.query,
        session_id: opts.sessionId,
      });
      if (status !== 200) return "";
      const records = Array.isArray(body.records)
        ? (body.records as Array<Record<string, unknown>>)
        : [];
      return recallToContext(records);
    } catch {
      return "";
    }
  }

  /** Record a finished turn into the caller's session memory. Fire-and-forget; the
   *  namespace is server-derived from the verified token. Returns true on success. */
  async record(opts: { sessionId: string; payload: MemoryTurn[] }): Promise<boolean> {
    try {
      const { status, body } = await this.post({
        op: "record",
        session_id: opts.sessionId,
        payload: opts.payload,
      });
      return status === 200 && body.recorded === true;
    } catch {
      return false;
    }
  }

  private async post(
    extra: Record<string, unknown>,
  ): Promise<{ status: number; body: Record<string, unknown> }> {
    const body = JSON.stringify({ idp_token: this.idpToken(), ...extra });
    const url = new URL(this.cfg.endpoint);
    const signer = new SignatureV4({
      service: "lambda",
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
    let parsed: Record<string, unknown> = {};
    try {
      parsed = (await resp.json()) as Record<string, unknown>;
    } catch {
      parsed = {};
    }
    return { status: resp.status, body: parsed };
  }
}
