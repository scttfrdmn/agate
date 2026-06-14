// Multimodal retriever (§10.2.7) — query-by-image and visual retrieval against the
// tenant's multimodal S3 Vectors index. Embeds with the Nova multimodal model
// (3072-dim) and queries the `<index>-mm` index. SigV4-signed with the broker-vended
// scoped credentials, so the credential bounds which index is readable — cross-tenant
// retrieval is denied at the resource, exactly as for the text path.

import {
  BedrockRuntimeClient,
  InvokeModelCommand,
} from "@aws-sdk/client-bedrock-runtime";
import { QueryVectorsCommand, S3VectorsClient } from "@aws-sdk/client-s3vectors";

import type { ScopedCredentials } from "../auth";
import { toSdkCredentials as sdkCreds } from "../auth/sdkCreds";
import { elementFromMetadata, type VisualElement } from "./citation";

// Must match the gate-verified contract (agate/multimodal.py, issue #17).
export const MM_EMBED_MODEL_ID = "amazon.nova-2-multimodal-embeddings-v1:0";
export const MM_EMBED_DIMENSION = 3072;

export interface MultimodalConfig {
  region: string;
  vectorBucketName: string;
  // The tenant's MULTIMODAL index, e.g. `agate-chem-mm` (the 3072-dim index).
  indexName: string;
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
  ) {}

  private async embed(body: Record<string, unknown>): Promise<number[]> {
    const client = new BedrockRuntimeClient({
      region: this.cfg.region,
      credentials: async () => sdkCreds(await this.creds()),
    });
    const res = await client.send(
      new InvokeModelCommand({ modelId: MM_EMBED_MODEL_ID, body: JSON.stringify(body) }),
    );
    return parseNovaEmbedding(JSON.parse(new TextDecoder().decode(res.body)));
  }

  private async query(vector: number[]): Promise<VisualMatch[]> {
    const client = new S3VectorsClient({
      region: this.cfg.region,
      credentials: async () => sdkCreds(await this.creds()),
    });
    const res = await client.send(
      new QueryVectorsCommand({
        vectorBucketName: this.cfg.vectorBucketName,
        indexName: this.cfg.indexName,
        topK: this.cfg.topK ?? 5,
        queryVector: { float32: vector },
        returnMetadata: true,
        returnDistance: true,
      }),
    );
    return (res.vectors ?? []).map((v) => {
      const md = (v.metadata ?? {}) as Record<string, unknown>;
      const sourceId = typeof md.source_key === "string" ? md.source_key : (v.key ?? "");
      return { element: elementFromMetadata(sourceId, md), distance: v.distance };
    });
  }

  // Query-by-image: retrieve visually similar figures across the corpus.
  async retrieveByImage(imageB64: string, imageFormat: string): Promise<VisualMatch[]> {
    const vector = await this.embed(novaEmbedBody({ imageB64, imageFormat }));
    return this.query(vector);
  }

  // Figure-aware text query against the multimodal index.
  async retrieveByText(text: string): Promise<VisualMatch[]> {
    const vector = await this.embed(novaEmbedBody({ text }));
    return this.query(vector);
  }
}
