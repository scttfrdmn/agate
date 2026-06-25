// Retriever — fetch in-scope context chunks from the broker-proxied retrieval
// Lambda (design §4, #84). The browser NO LONGER queries S3 Vectors directly: the
// scope filter is a real security boundary now, injected server-side from the
// VERIFIED token. This client just POSTs {idp_token, query} and renders the chunks.
//
// The Function URL is AWS_IAM-authed, so the POST is SigV4-signed with the
// broker-vended scoped credentials (same identity boundary as the chokepoint). The
// idp_token in the body is what the proxy verifies to DERIVE tenant/scope — the
// SigV4 creds only authorize invoking the endpoint. Two layers, like the chokepoint.

import { Sha256 } from "@aws-crypto/sha256-js";
import { SignatureV4 } from "@smithy/signature-v4";

import type { ScopedCredentials } from "../auth";
import { toSdkCredentials as sdkCreds } from "../auth/sdkCreds";
import type { RetrievedChunk } from "./context";

export interface RetrieverConfig {
  region: string;
  // The retrieval proxy's HTTP API URL (VITE_RETRIEVAL_URL). Empty disables RAG.
  endpoint: string;
  topK?: number;
}

export class Retriever {
  constructor(
    private readonly cfg: RetrieverConfig,
    private readonly creds: () => Promise<ScopedCredentials>,
    // The campus IdP token — the proxy verifies it to derive tenant/scope/courses.
    // Scope is NOT taken from any field this client controls.
    private readonly idpToken: () => string,
  ) {}

  async retrieve(query: string): Promise<RetrievedChunk[]> {
    const body = JSON.stringify({
      idp_token: this.idpToken(),
      query,
      top_k: this.cfg.topK ?? 5,
    });
    const url = new URL(this.cfg.endpoint);

    // service must be "execute-api" — the retrieval endpoint is an API Gateway HTTP
    // API with an IAM authorizer (not a Lambda Function URL). SigV4 with the wrong
    // service name would be rejected by the authorizer.
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
    if (!resp.ok) return []; // fail closed: no context rather than an error in chat
    const payload = (await resp.json()) as { chunks?: unknown };
    const chunks = Array.isArray(payload.chunks) ? payload.chunks : [];
    return chunks.map((c) => {
      const o = (c ?? {}) as Record<string, unknown>;
      return {
        key: typeof o.key === "string" ? o.key : "",
        text: typeof o.text === "string" ? o.text : "",
        sourceKey: typeof o.sourceKey === "string" ? o.sourceKey : undefined,
        sourceSystem: typeof o.sourceSystem === "string" ? o.sourceSystem : undefined,
        sourceItem: typeof o.sourceItem === "string" ? o.sourceItem : undefined,
        distance: typeof o.distance === "number" ? o.distance : undefined,
      };
    });
  }
}
