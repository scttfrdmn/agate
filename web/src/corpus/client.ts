// Corpus client (#191) — upload + list a user's own in-scope documents.
//
// POSTs {idp_token, action, ...} to the corpus Function URL (AWS_IAM auth), SigV4-signed
// (service "lambda") with the broker-vended scoped creds — same identity boundary as the
// drafting/deploy/chokepoint paths. The endpoint derives tenant/scope from the verified
// idp_token and reads/writes/lists ONLY within that fence; this client controls nothing
// about where a doc lands (a body tenant/scope/key is ignored server-side).

import { Sha256 } from "@aws-crypto/sha256-js";
import { SignatureV4 } from "@smithy/signature-v4";

import type { ScopedCredentials } from "../auth";
import { toSdkCredentials as sdkCreds } from "../auth/sdkCreds";

export interface CorpusDoc {
  name: string; // path within the scope subtree (e.g. "wk3/notes.pdf")
  key: string; // full S3 key
  size: number;
  modified: string | null;
}

export interface CorpusListResult {
  ok: boolean;
  reason: string;
  documents: CorpusDoc[];
  prefix: string;
}

export interface CorpusUploadResult {
  ok: boolean;
  reason: string;
  key: string;
  bytes: number;
}

export interface CorpusConfig {
  region: string;
  endpoint: string; // VITE_CORPUS_URL; empty disables the Corpus screen
}

// Saved-notebook metadata (list) and load result (#200 slice 4). Notebooks live under a
// reserved `_notebooks/` prefix in the same corpus fence — never surfaced as documents.
export interface SavedNotebookMeta {
  id: string;
  key: string;
  size: number;
  modified: string | null;
}

export interface NotebookListResult {
  ok: boolean;
  reason: string;
  notebooks: SavedNotebookMeta[];
}

export interface NotebookSaveResult {
  ok: boolean;
  reason: string;
  key: string;
}

export interface NotebookLoadResult {
  ok: boolean;
  reason: string;
  notebook: unknown; // the caller validates/deserialises the shape
}

// Pure: map the list endpoint's status + body to a CorpusListResult. A non-200 becomes a
// rejected result with a readable reason (rendered inline), never a thrown error.
export function responseToList(status: number, payload: Record<string, unknown>): CorpusListResult {
  if (status !== 200) {
    return { ok: false, reason: errorReason(status, payload), documents: [], prefix: "" };
  }
  const docs = Array.isArray(payload.documents)
    ? payload.documents.map((d) => {
        const o = (d ?? {}) as Record<string, unknown>;
        return {
          name: typeof o.name === "string" ? o.name : "",
          key: typeof o.key === "string" ? o.key : "",
          size: typeof o.size === "number" ? o.size : 0,
          modified: typeof o.modified === "string" ? o.modified : null,
        };
      })
    : [];
  return {
    ok: payload.ok === true,
    reason: "",
    documents: docs,
    prefix: typeof payload.prefix === "string" ? payload.prefix : "",
  };
}

// Pure: map the upload endpoint's status + body to a CorpusUploadResult.
export function responseToUpload(
  status: number,
  payload: Record<string, unknown>,
): CorpusUploadResult {
  if (status !== 200) {
    return { ok: false, reason: errorReason(status, payload), key: "", bytes: 0 };
  }
  return {
    ok: payload.ok === true,
    reason: "",
    key: typeof payload.key === "string" ? payload.key : "",
    bytes: typeof payload.bytes === "number" ? payload.bytes : 0,
  };
}

// Pure: map the list_notebooks endpoint's status + body to a NotebookListResult.
export function responseToNotebookList(
  status: number,
  payload: Record<string, unknown>,
): NotebookListResult {
  if (status !== 200) {
    return { ok: false, reason: errorReason(status, payload), notebooks: [] };
  }
  const notebooks = Array.isArray(payload.notebooks)
    ? payload.notebooks.map((n) => {
        const o = (n ?? {}) as Record<string, unknown>;
        return {
          id: typeof o.id === "string" ? o.id : "",
          key: typeof o.key === "string" ? o.key : "",
          size: typeof o.size === "number" ? o.size : 0,
          modified: typeof o.modified === "string" ? o.modified : null,
        };
      })
    : [];
  return { ok: payload.ok === true, reason: "", notebooks };
}

// Pure: map the load_notebook endpoint's status + body to a NotebookLoadResult.
export function responseToNotebookLoad(
  status: number,
  payload: Record<string, unknown>,
): NotebookLoadResult {
  if (status !== 200) {
    return { ok: false, reason: errorReason(status, payload), notebook: null };
  }
  return { ok: payload.ok === true, reason: "", notebook: payload.notebook ?? null };
}

function errorReason(status: number, payload: Record<string, unknown>): string {
  if (typeof payload.detail === "string") return payload.detail;
  if (typeof payload.error === "string") return payload.error;
  return `error ${status}`;
}

export class CorpusClient {
  constructor(
    private readonly cfg: CorpusConfig,
    private readonly creds: () => Promise<ScopedCredentials>,
    private readonly idpToken: () => string,
  ) {}

  async list(): Promise<CorpusListResult> {
    const payload = await this.post({ action: "list" });
    return responseToList(payload.status, payload.body);
  }

  /** Upload one file. `content` is the raw bytes; encoded base64 for the JSON envelope. */
  async upload(filename: string, content: ArrayBuffer, contentType: string): Promise<CorpusUploadResult> {
    const payload = await this.post({
      action: "upload",
      filename,
      content_type: contentType,
      content: bytesToBase64(new Uint8Array(content)),
    });
    return responseToUpload(payload.status, payload.body);
  }

  /** Save a notebook (JSON) under the session's fenced `_notebooks/` prefix (#200 slice 4). */
  async saveNotebook(notebookId: string, notebook: unknown): Promise<NotebookSaveResult> {
    const p = await this.post({ action: "save_notebook", notebook_id: notebookId, notebook });
    if (p.status !== 200) {
      return { ok: false, reason: errorReason(p.status, p.body), key: "" };
    }
    return { ok: p.body.ok === true, reason: "", key: typeof p.body.key === "string" ? p.body.key : "" };
  }

  /** List the session's saved notebooks. */
  async listNotebooks(): Promise<NotebookListResult> {
    const p = await this.post({ action: "list_notebooks" });
    return responseToNotebookList(p.status, p.body);
  }

  /** Load one saved notebook by id. */
  async loadNotebook(notebookId: string): Promise<NotebookLoadResult> {
    const p = await this.post({ action: "load_notebook", notebook_id: notebookId });
    return responseToNotebookLoad(p.status, p.body);
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

// Base64-encode bytes without blowing the call stack on large files (chunked).
export function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}
