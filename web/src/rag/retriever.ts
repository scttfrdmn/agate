// Retriever — embed a query and run a scoped S3 Vectors query against the user's
// own tenant index (design §4, §12 Phase 3). Both calls are SigV4-signed with the
// broker-vended scoped credentials, so the credential itself bounds which index is
// readable: cross-tenant retrieval is denied at the resource, not in this code.

import {
  BedrockRuntimeClient,
  InvokeModelCommand,
} from "@aws-sdk/client-bedrock-runtime";
import {
  QueryVectorsCommand,
  S3VectorsClient,
} from "@aws-sdk/client-s3vectors";

import type { ScopedCredentials } from "../auth";
import { toSdkCredentials as sdkCreds } from "../auth/sdkCreds";
import type { RetrievedChunk } from "./context";

// Must match the ingest contract (infra/stacks/data.py EMBED_MODEL_ID/DIMENSION).
export const EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0";
export const EMBED_DIMENSION = 1024;

export interface RetrieverConfig {
  region: string;
  vectorBucketName: string;
  // The tenant's index name, e.g. `agate-chem`. Derived from the session scope; the
  // credential can only read the index its agate:tenant tag matches.
  indexName: string;
  topK?: number;
}


export class Retriever {
  constructor(
    private readonly cfg: RetrieverConfig,
    private readonly creds: () => Promise<ScopedCredentials>,
  ) {}

  private async embed(query: string): Promise<number[]> {
    const client = new BedrockRuntimeClient({
      region: this.cfg.region,
      credentials: async () => sdkCreds(await this.creds()),
    });
    const res = await client.send(
      new InvokeModelCommand({
        modelId: EMBED_MODEL_ID,
        body: JSON.stringify({
          inputText: query,
          dimensions: EMBED_DIMENSION,
          normalize: true,
        }),
      }),
    );
    const payload = JSON.parse(new TextDecoder().decode(res.body));
    return payload.embedding as number[];
  }

  async retrieve(query: string): Promise<RetrievedChunk[]> {
    const vector = await this.embed(query);
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
      return {
        key: v.key ?? "",
        text: typeof md.text === "string" ? md.text : "",
        sourceKey: typeof md.source_key === "string" ? md.source_key : undefined,
        distance: v.distance,
      };
    });
  }
}
