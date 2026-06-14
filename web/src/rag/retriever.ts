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
  // The session's enrolled courses (agate:courses). Retrieval narrows to these
  // courses' material plus tenant-wide docs; omitting it (or []) hides course
  // material and returns only tenant-wide docs (fail-closed on enrollment).
  courses?: string[];
}

// Build the S3 Vectors metadata filter scoping retrieval to enrolled courses.
// A chunk is in scope when it has no `course` metadata (tenant-wide) OR its course
// is one the session is enrolled in. Mirrors agate.rag.course_filter (Python) so the
// browser and any server-side retriever scope identically.
export function courseFilter(courses?: string[]): Record<string, unknown> {
  const enrolled = (courses ?? []).filter(Boolean);
  const tenantWide = { course: { $exists: false } };
  if (!enrolled.length) return tenantWide;
  return { $or: [tenantWide, { course: { $in: enrolled } }] };
}

// Hierarchical scope filter (#70). A chunk is in scope when it is tenant-wide
// (neither `course` nor `scope_ancestors` set) OR the session sits at/above it in
// the tree (one of its scope nodes is in the chunk's `scope_ancestors` list) OR —
// for docs written under the flat model — its `course` matches a node. Mirrors
// agate.rag.scope_filter; tolerant of both old (course) and new (scope) docs.
export function scopeFilter(nodes?: string[]): Record<string, unknown> {
  const scope = (nodes ?? []).filter(Boolean);
  const tenantWide = { scope_ancestors: { $exists: false } };
  if (!scope.length) {
    // No scope nodes -> only tenant-wide docs (fail-closed). Also exclude flat
    // course docs by requiring no course either.
    return { $and: [tenantWide, { course: { $exists: false } }] };
  }
  return {
    $or: [
      { $and: [tenantWide, { course: { $exists: false } }] },
      { scope_ancestors: { $in: scope } },
      { course: { $in: scope } }, // backward-compat with flat course docs
    ],
  };
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
        // Narrow to the session's scope nodes (hierarchy #70) + tenant-wide docs.
        // The tenant index already bounds what the credential can read; this scopes
        // by subtree. courses are the session's scope nodes today (flat leaves).
        // (Cast: the SDK types `filter` as the loose DocumentType.)
        filter: scopeFilter(this.cfg.courses) as unknown as Record<string, never>,
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
