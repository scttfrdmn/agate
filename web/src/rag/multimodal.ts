// Multimodal retriever (§10.2.7) — query-by-image and visual retrieval. Like the
// text path (#84), retrieval goes through the broker-proxied retrieval Lambda, NOT
// a direct S3 Vectors call: the proxy embeds with Nova server-side, queries the
// `agate-{tenant}-mm` index, and injects the scope filter from the VERIFIED token
// (#94). The browser holds no s3vectors grant, so sub-tenant scope is a real
// boundary here too — a modified client can't omit the filter or pick another index.

import { Sha256 } from "@aws-crypto/sha256-js";
import { SignatureV4 } from "@smithy/signature-v4";

import type { ScopedCredentials } from "../auth";
import { toSdkCredentials as sdkCreds } from "../auth/sdkCreds";
import { elementFromMetadata, type VisualElement } from "./citation";

// Must match the gate-verified contract (agate/multimodal.py, issue #17). The model
// id is used SERVER-SIDE now; kept exported for the pure body-builder tests.
export const MM_EMBED_MODEL_ID = "amazon.nova-2-multimodal-embeddings-v1:0";
export const MM_EMBED_DIMENSION = 3072;

export interface MultimodalConfig {
  region: string;
  // The broker-proxied retrieval endpoint (same as the text Retriever, #84/#94).
  endpoint: string;
  topK?: number;
}

export interface VisualMatch {
  element: VisualElement;
  distance?: number;
}


// Build the Nova SINGLE_EMBEDDING body for a text or image query. Pure — exported
// for unit testing without the SDK. Exactly one of text/image must be provided.
export function novaEmbedBody(input: {
  text?: string;
  imageB64?: string;
  imageFormat?: string;
  purpose?: "GENERIC_INDEX" | "GENERIC_QUERY";
}): Record<string, unknown> {
  const { text, imageB64, imageFormat, purpose = "GENERIC_QUERY" } = input;
  if ((text == null) === (imageB64 == null)) {
    throw new Error("provide exactly one of text or imageB64");
  }
  const params: Record<string, unknown> = { embeddingPurpose: purpose };
  if (text != null) {
    params.text = { truncationMode: "END", value: text };
  } else {
    if (!imageFormat) throw new Error("imageFormat is required with imageB64");
    params.image = { format: imageFormat, source: { bytes: imageB64 } };
  }
  return { taskType: "SINGLE_EMBEDDING", singleEmbeddingParams: params };
}

// Pure: pull the 3072-dim vector out of a Nova response.
export function parseNovaEmbedding(payload: { embeddings?: Array<{ embedding?: number[] }> }): number[] {
  const vec = payload.embeddings?.[0]?.embedding;
  if (!vec || vec.length === 0) throw new Error("Nova response carried no embedding");
  return vec;
}

export class MultimodalRetriever {
  constructor(
    private readonly cfg: MultimodalConfig,
    private readonly creds: () => Promise<ScopedCredentials>,
    // The campus IdP token — the proxy verifies it to derive tenant/scope. The query
    // CONTENT (text/image) is client-supplied; the access boundary is not.
    private readonly idpToken: () => string,
  ) {}

  // POST to the retrieval proxy with index_kind="mm". Returns visual matches mapped
  // from the proxy's scope-filtered results.
  private async query(payload: Record<string, unknown>): Promise<VisualMatch[]> {
    // payload carries only the query CONTENT (query/image_*); spread it FIRST so the
    // fixed fields below can't be overridden by a caller-supplied key.
    const body = JSON.stringify({
      ...payload,
      idp_token: this.idpToken(),
      index_kind: "mm",
      top_k: this.cfg.topK ?? 5,
    });
    const url = new URL(this.cfg.endpoint);
    // service "execute-api" — the endpoint is an IAM-authed API Gateway HTTP API.
    const signer = new SignatureV4({
      service: "execute-api",
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
    if (!resp.ok) return []; // fail closed: no matches rather than an error
    const data = (await resp.json()) as { matches?: unknown };
    const matches = Array.isArray(data.matches) ? data.matches : [];
    return matches.map((m) => {
      const o = (m ?? {}) as Record<string, unknown>;
      const sourceId = typeof o.sourceId === "string" ? o.sourceId : "";
      // Reuse the pure metadata mapper for modality/ref/thumb consistency.
      const element = elementFromMetadata(sourceId, {
        modality: o.modality,
        ref: o.ref,
        thumb: o.thumb,
      });
      return { element, distance: typeof o.distance === "number" ? o.distance : undefined };
    });
  }

  // Query-by-image: retrieve visually similar figures across the corpus.
  async retrieveByImage(imageB64: string, imageFormat: string): Promise<VisualMatch[]> {
    return this.query({ image_b64: imageB64, image_format: imageFormat });
  }

  // Figure-aware text query against the multimodal index.
  async retrieveByText(text: string): Promise<VisualMatch[]> {
    return this.query({ query: text });
  }
}
