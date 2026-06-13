# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- **SEC-4 — real JWT verification replaces the Phase-1 placeholder across all entry
  points.** A re-review of the SEC-1/2 fixes found they had relocated trust to inputs
  whose trustworthiness wasn't established: the chokepoint reused the broker's
  unsigned-token placeholder behind a live Function URL (SEC-4a), and the agent
  derived its tier from an unsourced `X-Agg-Verified-Tier` header (SEC-4b).
  - `agg/jwt_verify.py`: one shared real verifier — RS256 against the IdP JWKS,
    pinned algorithm (no `alg=none`/HS-confusion), `iss`/`aud`/`exp`/`sub` enforced,
    JWKS client injectable for tests. Used by the broker, the choke point, and the
    agent so verification can't drift between them.
  - The broker and choke point now verify the token (no unsigned-JSON path); the
    agent derives its tier from the verified token (the forgeable header is gone),
    fail-closing to the cheapest tier. Every failure denies (no vend / 4xx).
  - `infra/assets.py` `pip_bundled_code()` bundles PyJWT into the broker, choke
    point, and LTI Lambda assets (and the agent Dockerfile installs it), so the
    verifier has its dependency at runtime; a missing dep fails closed at import.
  - Tests: real RS256 tokens (in-test keypair) covering tamper/expiry/wrong-aud/
    wrong-iss/`alg=none`/missing-claim rejection, plus regressions proving the agent
    falls back to oss on an unverifiable token and the broker/chokepoint deny.
- Security review pass (3 HIGH findings fixed before any demo deploy). The Tier 0
  chat path reviewed clean; all three findings were the newer paths trusting
  client/token-supplied scope instead of re-deriving it from a verified identity.
  - **SEC-1 — Tier 1 choke point authority confusion (`chokepoint/handler.py`).** It
    read `tenant`/`user`/`tier`/`courses`/`budget` from the request body and stamped
    them into STS session tags — allowing ABAC tag forgery, budget bypass (omit the
    field → no cap), and spend mis-attribution. Now identity is derived from the
    validated IdP token via `claims_to_tags` (same path as the broker), budget is
    looked up server-side from a new `agg-budget` table keyed by the verified
    identity, and input tokens are always estimated server-side. The body carries
    only `idp_token` + `model`/`messages`/`max_tokens`.
  - **SEC-2 — agent path had no tenant/tier enforcement (`agg/agent_dispatch.py`,
    `agent/server.py`, `infra/stacks/agent.py`).** The container invoked any model
    the payload named, and the Runtime execution role granted Bedrock + S3 Vectors on
    `Resource:*`. Dispatch now rejects any model outside the verified caller's tier
    (`allowed_models`, from the inbound-JWT `agg:tier`, fail-closed to oss); the
    execution role is scoped to agg's entitled model ARNs and this deployment's
    vector/docs bucket ARNs.
  - **SEC-3 — LTI tenant fallback (`agg/lti.py`).** A registration without a `tenant`
    fell back to the LTI context claim (instructor-controlled), enabling cross-tenant
    access on a shared LMS. The fallback is removed; a missing registration tenant
    now fails closed (`LtiClaimError`).

### Changed
- Refactor pass (no behavior change; demo-readiness #35–#37): removed three
  duplications surfaced now that the build is complete.
  - `agg/contracts.py` defines the `Backend` / `CostMeter` Protocols + `Emit` /
    `Usage` aliases once; the panel, analyze, and router orchestrators import them
    instead of each redeclaring their own.
  - `meter.read_spend_item()` is the single spend-table accessor (shared key format);
    the spend Lambda and the Tier 1 choke point both use it rather than two copies.
  - `web/src/auth/sdkCreds.ts` is the one `ScopedCredentials` → SDK-credentials
    adapter; the five transport/RAG clients import it instead of copy-pasting.

### Added
- Audit — CloudTrail management-plane trail (`infra/stacks/audit.py`): a multi-region
  `cloudtrail.Trail` with log-file validation, writing management events (role
  assumption, config changes) to the audit bucket under a `cloudtrail/` prefix. It
  is the forensic complement to the data-plane Bedrock invocation logs — together
  they give the per-identity "prove who accessed what" trail (design §8). The Trail
  construct's delivery bucket-policy statements compose with the existing Bedrock
  log-delivery statement on the same bucket. NO CLOCKS (storage-priced).
- Phase 5 governance tail — Guardrails + AgentCore Policy (Cedar):
  - `policy/cedar.py`: pure generation of the Cedar policy set (§13.4) from the SAME
    `agg.entitlements` table that drives the IAM model-access policy — a per-tier
    `InvokeModel` permit (tier+tenant matched), a tenant/course-scoped `Retrieve`
    permit, a per-user `CallTool` permit, and a defence-in-depth cross-tenant
    `forbid`. The human-auditable layer and the enforced IAM layer cannot drift.
  - `infra/stacks/governance.py`: a Bedrock `CfnGuardrail` (content filters across
    the standard categories + PROMPT_ATTACK on input, PII anonymization) and an
    AgentCore `CfnPolicyEngine` + `CfnPolicy` loaded with the generated Cedar text.
    L1 `Cfn*` (no L2 yet; migration tracked in #22). NO CLOCKS — Guardrails bill
    per-use, the policy engine is config.
  - Tests (no AWS): every tier covered, the Cedar model set mirrors the entitlement
    table, retrieval/tool/forbid clauses present; `cdk synth` confirms the Cedar text
    and the 6 content filters land in the template.
- Phase 6 — optional Tier 1 choke point (exact pre-call budget enforcement):
  - `cost/precall.py`: pure `evaluate_precall` / `estimate_call_cost` — reject a call
    *before* it runs when its **worst-case** cost (input tokens + `max_tokens` at the
    model rate) plus authoritative spend would exceed budget. Strictly stricter than
    the soft cap (which only declines the *next* call once over); fails closed on a
    zero/negative budget or invalid spend.
  - `chokepoint/handler.py`: the Tier 1 Lambda — reads authoritative spend from the
    `spend` table, runs the pre-call gate (a budget rejection returns **402** and the
    model is never invoked), and on allow invokes Converse **assuming the user's own
    scoped role** (same ABAC as Tier 0, plus enforcement).
  - `infra/stacks/chokepoint.py`: a Lambda **Function URL** (response streaming,
    AWS_IAM-authed) — no ALB, no always-on container, no clock. Built only when an
    institution opts into Tier 1; default deployments omit it.
  - `web/src/transport/openai.ts`: the Tier 1/2 transport implemented — SigV4-signed
    fetch of the Function URL with the scoped creds; pure `buildRequestBody` /
    `responseToChunks` (incl. surfacing a 402 budget rejection as terminal text).
  - Tests (fakes only, Python + TypeScript): the pre-call gate matrix incl. "stricter
    than soft cap", the handler rejecting before any model call, request/response
    mapping, and the 402 path.
- Phase 5 — governance/audit + authoritative spend (completes the §12 Phase 5
  metering arc; the soft cap now has a real, log-derived number to enforce):
  - `meter/parse.py`: pure, AWS-free translation of a Bedrock model-invocation log
    record into a priced `SpendRecord` — attributes tenant/user from the assumed-role
    identity + `agg:tenant` tag, derives the `{tenant}#{user}#{period}` (+ rollup)
    spend-table keys (§13.6), and prices via the shared `cost` engine. Fully tested.
  - `meter/handler.py`: the S3-triggered spend Lambda — reads invocation-log objects
    (incl. gzip), recomputes **authoritative** spend, and atomically increments the
    per-user and tenant-rollup rows; `read_spend()` is the helper the broker calls at
    credential refresh for the soft cap. One bad record never aborts the batch.
  - `infra/stacks/audit.py`: a restricted audit log bucket (Bedrock-delivery resource
    policy), the `spend` DynamoDB table (on-demand), the spend Lambda + S3 trigger,
    and an `AwsCustomResource` enabling Bedrock invocation logging (account-level
    config with no CFN resource type); stack-level cost-allocation tag. NO CLOCKS.
  - Tested end-to-end (fakes, no AWS): metering a log object increments both rows,
    and `evaluate_soft_cap` denies/allows against the resulting authoritative spend.
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
- Academic interaction model (§10.2) — spec added under `docs/`.
- Academic interaction model — Phase 1 (event protocol + SPA panes):
  - `web/src/events/protocol.ts`: the run event protocol — the `pane` field on
    `model`, and the new `divergence`, `citation`, and `artifact` events, alongside
    the existing `route`/`answer`/`code`/`chart`/`cost`/`receipt`/`guardrail`/
    `policy_denied` events. Additive and backward-compatible; transport-agnostic
    `Emit` contract (browser, CLI runner, and test collector share it).
  - `web/src/events/collector.ts`: an `EventCollector` sink plus a pure
    `reduce()` / `runStateFrom()` that folds an ordered event stream into render
    state (Panel panes keyed by label, single-stream Ask answer, Analyze cells,
    divergence, running cost). Unknown event types pass through (forward-compatible).
  - `web/src/panes/render.ts`: framework-free rendering of the multi-pane Panel
    layout, the side-by-side divergence view (agreement/disagreement/unsupported
    with a verify flag), and the notebook-style editable, re-runnable Analyze cell.
  - Vitest tests (fakes only, no AWS): the event collector, the reducer over Panel/
    Ask/Analyze runs, backward-compatibility with unknown events, and the divergence
    and Analyze-cell rendering.
- Academic interaction model — Phase 2 (Panel orchestration + adjudicator contract):
  - `agg/panel/schema.py`: the `Divergence` Pydantic model mirroring the §10.2.5
    draft-07 schema (forbids extra properties, requires ≥1 position per claim,
    constrains the stance/kind enums) plus `strip_fences()` for accidental Markdown
    fences around the adjudicator's JSON.
  - `agg/panel/prompts.py`: the `ADJUDICATE_SYSTEM` prompt (structured-only output)
    and a default review prompt; reviewer labels are roster config, kept neutral.
  - `agg/panel/orchestrator.py`: `run_panel` — N roster members review the same
    evidence in parallel over injected `Backend`/`CostMeter` interfaces (no AWS in
    core), each emitting its own `model` start/done + per-pane `cost`; the
    adjudication tail validates the structured output and emits a `divergence`
    event, falling back to an unstructured `answer` on malformed/invalid output.
  - Tests (fakes only, no AWS): per-pane events, identical evidence to every
    member, a well-formed adjudication whose `pane` values are a subset of the
    roster labels, cost accumulation, and the malformed- **and** schema-invalid-
    adjudicator fallback paths.
- Academic interaction model — Phase 3 (Analyze cell / Code Interpreter):
  - `agg/analyze/schema.py`: pure, AWS-free `extract_code()` (pulls the Python
    script from the model's fenced output), `parse_invoke_result()` (normalises an
    AgentCore `InvokeCodeInterpreter` stream result into text/image content blocks),
    and `result_to_events()` (maps a result to `answer`/`chart` events, surfacing a
    traceback on error rather than swallowing it).
  - `agg/analyze/prompts.py`: the `ANALYZE_SYSTEM` prompt (one self-contained,
    network-free Python script; chart saved to a known path for inline rendering).
  - `agg/analyze/orchestrator.py`: `run_analyze` over injected `Backend` /
    `CodeRunner` (the Code Interpreter microVM) / `CostMeter` — generate → emit the
    editable `code` cell → execute in the sandbox → emit `answer`/`chart`. Execution
    time is metered as a **compute** line distinct from token cost (`add_compute`),
    and the re-run path accepts edited code and skips generation.
  - Tests (fakes only, no AWS): generate→code→chart ordering, the re-run path
    skipping generation, compute-vs-token metering separation, and the error path.
- Academic interaction model — Phase 4 (multimodal knowledge base):
  - §10.2.7 Phase-0 verification gate completed first (issue #17): confirmed
    `amazon.nova-2-multimodal-embeddings-v1:0` (TEXT/IMAGE/AUDIO/VIDEO → embedding,
    **3072-dim**), S3 Vectors as a first-class Bedrock-KB backend
    (`s3_vectors_configuration`), and the supplemental multimodal-storage location.
  - `agg/multimodal.py`: pure, AWS-free helpers — the Nova embedding request/response
    mapping, ingestion-path selection (native multimodal vs parser+text fallback),
    and visual-citation resolution (`citation` event + figure/table corpus deep links).
  - `infra/stacks/data.py`: a **3072-dim multimodal S3 Vectors index per tenant**,
    built alongside (not replacing) the 1024-dim text index, sharing the tenant CMK
    and `agg:tenant` tag; a `_mm-artifacts/` supplemental-storage prefix. Embedding
    dimension is now per-index config, not a global constant.
  - `web/src/rag/multimodal.ts` + `citation.ts`: query-by-image and figure-aware
    text retrieval against the multimodal index, and visual-citation/deep-link
    resolution mirroring the Python helpers.
  - Tests (fakes only, no AWS) across Python and TypeScript: embedding request
    shapes, path selection, the 3072-vs-1024 dimension guard, and citation/deep-link
    resolution for text vs figure vs table.
- Academic interaction model — Phase 5 (reproducible run artifact):
  - `agg/artifact.py`: pure, AWS-free `RunArtifact` Pydantic model + `build_artifact()`
    that folds a run's typed event stream into one shareable, citable record — mode,
    question, panel roster, models used, transcript, generated code, citations (text
    and visual), the divergence structure (reusing the `Divergence` model), and the
    receipt. `receipt_to_csv()` exports a chargeback-tagged receipt (grant/course
    code) and `to_json()` the canonical record; `created_at`/`run_id` are inputs
    (no clock in core).
  - `web/src/events/artifact.ts`: a browser "save run" `serializeRun()` mirroring the
    Python artifact shape, plus a matching `receiptToCsv()` (CSV-escaped, tagged).
  - Tests (fakes only) across Python and TypeScript: full Panel/Analyze stream
    capture, deduped models in first-seen order, JSON round-trip, canonical
    none-omission, the cost-tag CSV export, and forward-compat with unknown events.
- Academic interaction model — Phase 6 (router + mode override), completing §10.2:
  - `agg/router.py`: pure `classify_mode()` (a router model's one-word reply →
    SYNTHESIS/DEBATE/ANALYSIS, robust to noise, cue-word fallback, never escalating
    past the cheapest default) and `resolve_mode()` (explicit override wins). The
    `run_router` orchestration makes the cheap routing call (one fast model,
    `max_tokens≈5`) over an injected `Backend`/`CostMeter`, meters it, and emits a
    `route` event — **never an `answer`**; an explicit override short-circuits with
    no call and no spend.
  - `web/src/router.ts`: the UI↔wire mode mapping (`ask`/`panel`/`analyze` ↔
    `SYNTHESIS`/`DEBATE`/`ANALYSIS`) and override-precedence resolution for the
    explicit mode control.
  - Tests (fakes only): classification of tokens/cues/ambiguous input, override
    precedence, and that the routing call is metered, emits only `route`, and is
    skipped entirely when the user forces a mode.
- Phase 8 — agent path: AgentCore Runtime deploy stack (`infra/stacks/agent.py`):
  - `CfnRuntime` hosting the (already-built) Panel/Analyze/router orchestration as a
    framework-agnostic container, with `network_mode=PUBLIC` (no VPC) so it scales to
    zero with no idle clock; a `CfnRuntimeEndpoint`; and a `CfnCodeInterpreterCustom`
    (PUBLIC) sandbox for Analyze. L1 `Cfn*` (no L2 yet; migration tracked in #22).
  - Inbound auth via a Cognito `custom_jwt_authorizer` (OIDC discovery URL + app
    audience from deploy context) so the campus user's identity flows into the
    session; the Runtime's own execution role is the scoped outbound tool identity
    (Bedrock invoke + tenant S3 Vectors read). The agent container image and IdP
    coordinates are deploy-time context (PLACEHOLDER until supplied).
  - Wired into the CDK app; `cdk synth` verified for all four stacks (identity,
    data, lti, agent), with and without the Cognito authorizer context.
- Phase 8 — agent path: reference agent container + agent transport:
  - `agg/agent_dispatch.py`: pure, AWS-free `dispatch()` that resolves the mode
    (router or explicit override) and drives the matching orchestration — Ask, Panel
    (`run_panel`), or Analyze (`run_analyze`) — over injected `Backend`/`CodeRunner`/
    `CostMeter`, emitting the run event stream. Fully unit-tested with fakes.
  - `agent/`: the reference agent container — a stdlib HTTP server (`server.py`)
    honouring the AgentCore Runtime protocol (`POST /invocations`, `GET /ping`,
    port 8080) that decodes the payload, runs dispatch, and returns the event stream
    as newline-delimited JSON; Bedrock-backed `Backend`/`CodeRunner`/`CostMeter`
    adapters (`backends.py`); a Python 3.13 non-root `Dockerfile`. No web framework.
  - `web/src/transport/agentcore.ts`: the agent-path transport implemented —
    `InvokeAgentRuntime` with scoped credentials and a session id, decoding the
    NDJSON response blob into the shared `RunEvent` stream; also satisfies the
    `Transport` interface's `converse` for uniform tier handling.
  - Tests (fakes only, Python + TypeScript): mode routing/override, per-mode
    orchestration dispatch, error paths, the invocation→receipt flow, the NDJSON
    event codec, and the payload round-trip.
- Phase 5 — real-time metering: the `CostMeter` cost engine (`cost/`):
  - `cost/meter.py`: a pure, thread-safe `CostMeter` computing **actual dollars** per
    call from authoritative usage × rates, itemised into a `Receipt` (rows + total)
    that doubles as chargeback. One engine for LLM, embedding, retrieval (per-1k),
    and compute (per-second) lines; satisfies the `add_llm`/`add_compute`/`total`
    protocol the orchestration already calls, plus `add_embedding`/`add_retrieval`.
  - `cost/pricing.py`: a `PriceBook` resolving rates config-override → hard-default
    (and an optional live Price List fetch at the edge), so a missing rate never
    blocks a call. Respects the documented Price List quirks (S3 Vectors is config-
    only; FM pricing namespace; us-east-1-only API).
  - `cost/softcap.py`: the pure soft-cap decision (§7.1) — spend-vs-budget, failing
    closed on a zero/negative budget or an invalid (negative) spend; the broker reads
    authoritative spend at credential refresh and declines to vend model creds when
    over budget.
  - The reference agent now meters with the real `CostMeter` (replacing the
    placeholder), emitting an itemised `receipt` event to close each run.
  - `web/src/cost.ts`: the SPA port of the same engine for a live, **non-authoritative**
    running estimate (display only; the enforced number is computed server-side).
  - Tests (fakes only, Python + TypeScript): dollar math per kind, config-vs-default
    rate resolution, the S3-Vectors config fallback, thread-safe parallel metering,
    the soft-cap decision matrix, and Python/TS parity on a worked example.
- Phase 7 — the `agg` admin CLI (Go): real `tenant`, `budget`, `deploy`, and
  `ingest` commands replacing the Phase 0 stubs.
  - `internal/config`: a pure `.agg.json` model — tenant set (validated against the
    `agg:tenant` charset, kept sorted/deduped) and per-tenant budgets (which the
    soft cap reads). Load treats a missing file as empty; full table tests.
  - `internal/commands`: pure plan construction — `deploy` turns the tenant set into
    `cdk deploy ... -c tenants=...`; `ingest` targets the FERPA-correct
    `s3://agg-docs-…/{tenant}/<file>` prefix. Both return the exact argv, tested.
  - The cloud-mutating commands (`deploy`, `ingest`) **plan by default and run only
    with `--confirm`** — agg never changes cloud state implicitly. `budget set`
    accepts the natural `set <tenant> --usd N` ordering.
  - `gofmt`/`go vet` clean; `go test ./...` green across the three packages.

### Fixed
- **Phase 1 proven live** (first real AWS deploy, us-east-1): the identity stack
  deploys and the full chain works against real Bedrock — broker Lambda derives the
  `agg:` tags, vends scoped STS creds, and IAM/ABAC allows an entitled model and
  denies a non-entitled one (oss session: gpt-oss allowed / frontier denied;
  frontier session: both allowed, cumulative). Two corrections the live deploy
  surfaced:
  - Lambda asset packaging excluded `infra/cdk.out` but not the **root** `cdk.out`,
    causing a recursive self-copy (`ENAMETOOLONG`) on first deploy. Replaced the
    three divergent per-stack exclude lists with one shared `infra/assets.py`
    `LAMBDA_ASSET_EXCLUDES` covering `cdk.out`, caches, and other-language trees.
  - The entitlement table used stale/mis-typed model ids. Updated to ids verified
    present in-region, and — crucially — invoking a cross-region **inference
    profile** (Claude 4.x) requires `bedrock:InvokeModel` on **both** the profile
    ARN **and** the underlying foundation-model ARN; `model_resource_arns()` now
    emits both, region-wildcarded for the underlying FM.

[Unreleased]: https://github.com/scttfrdmn/aws-genai-gateway/commits/main
