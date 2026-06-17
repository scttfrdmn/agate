// Collaborative rooms client (#116 PR 2, the SPA surface for the rooms endpoint).
//
// A polling transport: the SPA calls open/join/leave/post over the IAM-authed Function URL and
// polls `events?since=<cursor>` to fold new messages into the view. The room's reach is enforced
// SERVER-side (intersection of members, attribution, per-member budget) — this client only
// renders the bounded view + posts attributed messages; it makes no authority decision.
//
// Same SigV4 Function-URL pattern as the drafting/authoring clients (service "lambda" with the
// broker-vended scoped creds + the verified idp_token in the body).

import { Sha256 } from "@aws-crypto/sha256-js";
import { SignatureV4 } from "@smithy/signature-v4";

import type { ScopedCredentials } from "../auth";
import { toSdkCredentials as sdkCreds } from "../auth/sdkCreds";

export interface RoomMember {
  kind: "human" | "agent";
  subject: string;
}

export interface RoomMessage {
  author: string;
  kind: "human" | "agent";
  text: string;
  actingAs?: Record<string, unknown>;
}

// The endpoint's room view (open/join/events): members + derived scope/tier + messages + cursor.
export interface RoomView {
  ok: boolean;
  reason?: string;
  room: string;
  scope: string;
  tier: string;
  members: RoomMember[];
  messages: RoomMessage[];
  cursor: number;
}

// A post outcome: the appended message + new cursor, or a budget/membership rejection.
export interface PostResult {
  ok: boolean;
  reason?: string;
  cursor?: number;
  message?: RoomMessage;
}

export interface RoomConfig {
  region: string;
  endpoint: string; // VITE_ROOMS_URL; empty disables the Rooms screen
}

// Pure: map a status + body to a RoomView (open/join/events). A non-200 → ok=false + reason.
export function toRoomView(status: number, payload: Record<string, unknown>): RoomView {
  const base: RoomView = {
    ok: false,
    reason: "",
    room: typeof payload.room === "string" ? payload.room : "",
    scope: typeof payload.scope === "string" ? payload.scope : "",
    tier: typeof payload.tier === "string" ? payload.tier : "",
    members: Array.isArray(payload.members) ? (payload.members as RoomMember[]) : [],
    messages: Array.isArray(payload.messages) ? (payload.messages as RoomMessage[]) : [],
    cursor: typeof payload.cursor === "number" ? payload.cursor : 0,
  };
  if (status !== 200) {
    const detail = typeof payload.detail === "string" ? payload.detail : undefined;
    const err = typeof payload.error === "string" ? payload.error : `error ${status}`;
    return { ...base, ok: false, reason: detail ?? err };
  }
  return { ...base, ok: payload.ok === true, reason: typeof payload.reason === "string" ? payload.reason : "" };
}

// Pure: map a status + body to a PostResult.
export function toPostResult(status: number, payload: Record<string, unknown>): PostResult {
  if (status !== 200) {
    const detail = typeof payload.detail === "string" ? payload.detail : undefined;
    return { ok: false, reason: detail ?? `error ${status}` };
  }
  return {
    ok: payload.ok === true,
    reason: typeof payload.reason === "string" ? payload.reason : "",
    cursor: typeof payload.cursor === "number" ? payload.cursor : undefined,
    message: (payload.message as RoomMessage) ?? undefined,
  };
}

export class RoomClient {
  constructor(
    private readonly cfg: RoomConfig,
    private readonly creds: () => Promise<ScopedCredentials>,
    private readonly idpToken: () => string,
  ) {}

  private async post(req: Record<string, unknown>): Promise<{ status: number; payload: Record<string, unknown> }> {
    const body = JSON.stringify({ ...req, idp_token: this.idpToken() });
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
    let payload: Record<string, unknown> = {};
    try {
      payload = (await resp.json()) as Record<string, unknown>;
    } catch {
      payload = {};
    }
    return { status: resp.status, payload };
  }

  async open(nonce: string): Promise<RoomView> {
    const { status, payload } = await this.post({ op: "open", nonce });
    return toRoomView(status, payload);
  }

  async join(room: string, agent?: string): Promise<RoomView> {
    const req: Record<string, unknown> = { op: "join", room };
    if (agent) req.agent = agent;
    const { status, payload } = await this.post(req);
    return toRoomView(status, payload);
  }

  async leave(room: string, member?: string): Promise<{ ok: boolean }> {
    const req: Record<string, unknown> = { op: "leave", room };
    if (member) req.member = member;
    const { status, payload } = await this.post(req);
    return { ok: status === 200 && payload.ok === true };
  }

  async postMessage(room: string, text: string): Promise<PostResult> {
    const { status, payload } = await this.post({ op: "post", room, text });
    return toPostResult(status, payload);
  }

  async events(room: string, since: number): Promise<RoomView> {
    const { status, payload } = await this.post({ op: "events", room, since });
    return toRoomView(status, payload);
  }
}
