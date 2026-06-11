# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Phase 0 — repository scaffold: CDK v2 Python app (`infra/`, `uv`-managed, Python 3.13),
  Go module for the `agg` CLI (`cli/`), Vite + TypeScript SPA skeleton (`web/`), and the
  component directories from design §11 (`policy/`, `cost/`, `meter/`, `lti/`, `agent/`,
  `ingest/`, `docs/`). `README.md`, this changelog, `.gitignore`, and `cdk bootstrap` notes.
- Phase 1 — identity broker + ABAC:
  - Pure, side-effect-free `claims_to_tags()` translation (the `agg:` session-tag scheme,
    §13.1) with full unit-test coverage and no AWS dependency.
  - Single-source-of-truth tier → entitled-model-ARN table, shared by the broker and the
    generated IAM model-access policy.
  - `infra/stacks/identity.py`: Cognito **Identity Pool** (federated SAML/OIDC, no User
    Pool), the authenticated role + permissions boundary keyed on `agg:` principal tags,
    and the per-request **broker Lambda** that validates the IdP token, derives the four
    tags, and vends scoped STS credentials.
  - Phase 1 end-to-end proof: IAM policy simulation asserting `Converse` is allowed for an
    entitled model ARN and denied for a non-entitled one, scoped purely by `agg:` tags.
- Phase 2 — static SPA, Tier 0 browser-direct transport:
  - `web/src/auth/credentials.ts`: `CredentialManager` that fetches scoped STS credentials
    from the broker and refreshes them before expiry, with a pure, unit-tested
    `shouldRefresh()` decision and concurrent-refresh coalescing.
  - `web/src/transport/bedrock.ts`: the Tier 0 adapter implemented — browser-direct
    `ConverseStream` signed with the scoped credentials, streaming answer text and a
    separate reasoning channel (for reasoning models such as gpt-oss), with final token
    usage for the non-authoritative client-side cost estimate.
  - `web/src/chat/session.ts`: in-memory `ChatSession` over the transport (no persistence
    yet); reasoning is shown live but never persisted to history.
  - Minimal streaming chat UI wired in `web/src/main.ts`; build-time config in
    `web/src/config.ts` (no secrets in the client).
  - Vitest unit tests for the refresh decision, message mapping, and chat accumulation;
    an opt-in live `ConverseStream` smoke test (`AGG_LIVE_SMOKE=1`), verified against
    Bedrock.
- Phase 3 — data plane (S3 Vectors RAG):
  - `infra/stacks/data.py`: an `agg-docs` S3 bucket (per-tenant prefix, versioned,
    retained), an S3 Vectors vector bucket with one **index per tenant** (1024-dim,
    cosine), and a **per-tenant KMS CMK** on each index. Each index is tagged with its
    `agg:tenant` so the Phase 1 ABAC data-scope policy isolates reads. Built on L1
    `Cfn*` constructs (no L2 for S3 Vectors yet).
  - `ingest/handler.py`: embed-on-upload Lambda — S3 `ObjectCreated` → chunk → Bedrock
    Titan embeddings → `PutVectors` into the tenant's index. Tenant is derived from the
    key prefix and fails closed; one bad object never aborts the batch.
  - `agg/rag.py`: pure, AWS-free chunking, tenant-key derivation, and vector-record
    assembly with full unit-test coverage.
  - `web/src/rag/`: query-embed → scoped `QueryVectors` on the tenant index → context
    injection; a pure, unit-tested context builder. RAG is opt-in via an optional
    `ContextProvider` on `ChatSession` (grounding is sent per turn, never persisted).
  - Tenant-isolation proof (IAM policy simulation): a `chem`-scoped session may
    `QueryVectors` its own index and is **denied** the `psych` index (both directions).
- Phase 4 — LTI 1.3 tool provider:
  - `agg/lti.py`: pure, AWS-free mapping of an LTI 1.3 launch (roles, context, NRPS)
    into the claims dict that the Phase 1 `claims_to_tags()` consumes — so LTI is one
    concrete source of `agg:affiliation` / `agg:courses`, with no second tag scheme.
    Instructor → faculty (mid tier), Learner → student (oss); plus pure nonce/state
    replay-protection decisions.
  - `lti/handler.py`: the four LTI 1.3 endpoints (`/lti/login`, `/lti/launch`,
    `/.well-known/jwks.json`, `/lti/deeplink`) on one Lambda. The launch path
    RS256-verifies the platform id_token against its JWKS and enforces
    `iss`/`aud`/`exp`/`nonce`/`state` (single-use), failing closed on every check;
    the validated claims are handed to the SPA for exchange at the Phase 1 broker.
  - `infra/stacks/lti.py`: HTTP API + Lambda + two DynamoDB on-demand tables
    (platform registrations; short-lived state/nonce with a TTL attribute). The
    Lambda asset bundles PyJWT+cryptography via a local (Docker-free) bundler.
  - Tests sign tokens with an in-test RSA keypair to exercise the real RS256 path:
    valid launch, replayed state, nonce mismatch, tampered signature, expired token,
    and wrong audience are all covered.

[Unreleased]: https://github.com/scttfrdmn/aws-genai-gateway/commits/main
