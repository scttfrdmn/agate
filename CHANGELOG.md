# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Effective-boundary view — render what an agent can touch / do / spend (#108, Phase
  10 / tracking #101).** Because agate *generates* the credential, it can tell a
  non-expert admin/author, in plain language, exactly what an agent is bounded to —
  solving the classic IAM tragedy where nobody knows what a policy actually grants.
  `agate/boundary.py` (pure) turns a `CompiledAgent` (or a per-invoker
  `InstantiatedAgent`) into an `EffectiveBoundary`: the models it may invoke (its tier's
  set), the `{tenant}/{scope}/` data path it can read, the tools it can use (read vs
  draft-write), and its spend ceiling — **plus the explicit denials** (cannot invoke a
  higher tier, cannot read outside its subtree, cannot use an undeclared tool). `summary()`
  gives human lines; `to_dict()` feeds the admin API / authoring UI (#117). It derives
  from the SAME compiled artifacts the credential is built from (tier/scope from the
  tags template), so it cannot drift; the per-invoker variant reads the *narrowed*
  `child_tags` so it never over-states a student's confined instance. A live
  `iam:SimulateCustomPolicy` drift proof (`tests/test_proof_boundary.py`) asserts every
  ALLOW the view claims is `allowed` in IAM and every DENIAL is denied — no gap between
  the explanation and the enforcement. A pre-merge security review confirmed the cardinal
  property: the view never *under*-states the boundary (no admin is surprised by an
  omitted capability). This is the trust surface behind graphical authoring (§8.5).
- **Per-invoker instantiation — one authored agent, scoped per invoker (#107, Phase 10
  / tracking #101).** The payoff of bounded delegation (#106): a professor authors **one**
  `chem101-ta` agent shared to a course; it instantiates per-invoker under each invoker's
  OWN verified credential — same agent, N students, each confined to *their own* data,
  with no app-layer "is this your submission?" check. The isolation is structural:
  `instantiate_for_invoker(invoker, spec)` returns `delegate(invoker, spec)` (#106), so
  invoker A's child is bounded by A and invoker B's by B — **disjoint by construction**.
  Eligibility (`is_eligible_invoker`) — "may this verified session run this agent?" — is
  read from the invoker's OWN verified tags (`roster:<course>` ⟺ course ∈
  `invoker.courses`; `scope:<path>` ⟺ scopes overlap), never a trusted/enumerated roster
  list, and fails closed. A live `iam:SimulateCustomPolicy` proof shows Alice (chem-101)
  and Bob (chem-202) running the same agent each read only their own course subtree and
  are `explicitDeny`'d on the other's. A pre-merge security review caught two issues,
  both fixed: the per-invoker memory/session namespace key was non-injective (`_clean_id`
  strips `/`, so subjects `a/b` and `ab` collided) — now disambiguated by a digest of the
  raw ids (load-bearing before #109/#110 persist memory by that key); and
  `tags._normalise_data_scope` now rejects `.`/`..` segments to match
  `budget.normalise_scope` (defense-in-depth). Pure core + proof; the live instantiation
  Lambda is deferred with the #106 spawn endpoint.
- **Bounded delegation — a spawned agent's credential narrows the spawner's (#106,
  Phase 10 / tracking #101).** The other half of the keystone (with the #105 compiler):
  when a principal spawns an agent, the agent runs under the **intersection** of the
  spawner's verified authority and the spec — `tier = min(spawner, spec)`, `scope =`
  subtree-containment intersection (the more-specific of the two; a disjoint conflict
  **refuses to spawn**, fail-closed), tenant held verbatim (cross-tenant is structurally
  impossible — a spec has no tenant field), courses inherited, and `role` forced to
  member (an agent is never an admin, even if the spawner is). So a spawned/triggered/
  collaborating agent is **never more privileged than the principal it acts for**, and
  it holds transitively across hops (`delegate(delegate(root, A), B)` only narrows —
  the basis for agent graphs, #111). `agate/delegate.py` is pure (`delegate`,
  `scope_intersect`, `delegate_budget`); the one AWS edge (`spawn_child`) takes its STS
  client as a param, so even it is fake-testable — same verify→tags→assume pattern as
  the broker, with `<tenant>@<subject>` attribution (#79) and transitive tags. A live
  `iam:SimulateCustomPolicy` proof (`tests/test_proof_delegation.py`) confirms the
  headline guarantee: a chemistry-scoped frontier spawner produces a child that cannot
  read physics, cannot read a sibling course it was narrowed below, and cannot invoke
  above its (min) tier. A pre-merge security review found no escalation path on any
  axis. The live spawn Lambda/CDK + real budget-row authorization are deferred (#107 /
  follow-up); this is the pure narrowing core + assume helper + proof.
- **Agent-spec schema + compiler — the keystone of the agent platform (#104, #105,
  Phase 10 / tracking #101).** An agent is now a declarative artifact that **compiles
  to a scoped identity** — the spec IS the agent's IAM, so a compiled agent cannot
  exceed it. `agate/agentspec.py` (pure, AWS-free) parses a `*.agate.yaml`-shaped dict
  into a validated `AgentSpec` (role, scope, reasoning, tools, memory, budget, invokers,
  triggers, visibility) with fail-closed validation (unknown keys/tools/garbled-scope/
  `..`/NaN-budget all rejected) and a reviewed capability catalog (tools are denied by
  absence). `agate/agentcompile.py` compiles it — **composing** the existing primitives,
  not duplicating them: `policy.generate.model_access_policy`/`data_scope_policy` + a new
  `agent_tool_policy` (each tool grant tenant+scope-fenced, writes confined to a
  `_drafts/` path), `agate.patterns.compile_pattern` for the reasoning payload, and
  `agate.budget` key-shape templates for the cascade budget rows. A live
  `iam:SimulateCustomPolicy` proof (`tests/test_proof_agent_policy.py`) confirms the
  compiled policies grant **exactly** the spec's tier + scope + tools and deny everything
  broader (higher-tier model, sibling/cross-tenant doc, undeclared write). A pre-merge
  security review caught and removed a self-escalation path: the spec had a `grant: true`
  field that could promote tier — now gone, because authority is a property of the
  verified spawner's credential (#106), never claimed by the artifact. The compiler is
  pure (no STS/DynamoDB); spawn-time narrowing + budget authorization are deferred to
  #106 bounded delegation.
- **Multimodal retrieval now goes through the scope-enforcing proxy too (#94, closes
  the bypass #84 left).** #84 routed *text* vector retrieval through the proxy; the
  *multimodal* path (`MultimodalRetriever`) still queried the `agate-{tenant}-mm`
  index directly from the browser with no scope filter (tenant-fenced only). The
  `agate-retrieval` proxy now serves both indexes, selected by an `index_kind`
  (`"text"`|`"mm"`) body field: for `mm` it embeds with Nova server-side
  (`agate.multimodal.nova_embed_request`), resolves `mm_index_name_for_tenant(tenant)`,
  and injects the **same** `scope_filter(retrieval_nodes(...))` derived from the
  verified token. The proxy's `bedrock:InvokeModel` grant is scoped to exactly the two
  embed models (Titan + Nova). The SPA's `MultimodalRetriever` SigV4-POSTs to the
  proxy (`index_kind:"mm"`) instead of calling S3 Vectors directly; the query content
  (text or image) is client-supplied (it's what the user searches for), but tenant,
  scope, and index are token-derived. With this, no browser path reaches `QueryVectors`
  for either index — vector sub-tenant scope is a real boundary across the board.
- **Vector sub-tenant scope is now a REAL boundary — broker-proxied retrieval (#84,
  completes #70 phase 4).** #80 made hierarchical scope IAM-enforced for S3 documents,
  but vectors stayed advisory: the browser signed `QueryVectors` directly and supplied
  the scope `filter` itself, so a modified client could omit it and read the whole
  tenant index (tenant was fenced; sub-tenant was not). Now the browser-held
  `agate-authenticated` role has **no** `s3vectors` grant at all; vector retrieval goes
  through a server-side proxy Lambda (`agate-retrieval`) that derives the scope filter
  from the VERIFIED token (`scope_filter(retrieval_nodes(tags.scope, tags.courses))`)
  and assumes a dedicated, tenant-tag-fenced `agate-vector-reader` role — the **only**
  identity that can query vectors, and one the browser cannot assume. The proxy embeds
  server-side (Titan) and ignores any `tenant`/`scope`/`filter` field in the request
  body. **Boundary split, by design:** tenant stays IAM-enforced (`agate-vector-reader`
  fenced by `ResourceTag==PrincipalTag`); sub-tenant scope is enforced by the proxy
  code because it **cannot** be IAM-enforced (per-tenant index, row-metadata scope) —
  what makes it real is that no client path can reach `QueryVectors` to omit the filter.
  Pure `agate.rag.retrieval_nodes` is the tested seam; live `iam:SimulateCustomPolicy`
  proves the browser role is now denied `QueryVectors` even for its own tenant, while
  the reader role keeps the cross-tenant deny. The SPA POSTs `{idp_token, query}` to the
  proxy (SigV4-signed, `VITE_RETRIEVAL_URL`); the direct S3 Vectors path is gone.
  **Multimodal retrieval keeps the old direct/tenant-fenced-only path for now (#94).**
- **Budget-table writer — admin-gated budget authoring (#87, splits from #81).** The
  #81 cascade READS budget rows from `agate-budget` but nothing in agate WROTE them
  (seeded by hand). The governed-access console's admin Lambda now takes a
  `op:"set_budget"` mutation that authors a budget row in the EXACT key shape the
  chokepoint reads — tenant (`tenant#period`), per-user (`tenant#user#period`), or
  scope-node (`tenant#scope#<node>#period`). Gated at the SAME credential boundary as
  everything else: `agate:role == admin` from the verified campus token; identity
  (tenant + admin_scope) is read from the token, never the request body. The pure
  `agate.budget.plan_budget_write` does all validation/authorization: **no cross-tenant
  writes** (target tenant must equal the admin's own), and a **scoped admin is confined
  to its own subtree** (segment-wise containment — `chem` does not match `chemistry`;
  scoped admins cannot set tenant- or user-level budgets). Keys are rebuilt in `agate`
  (can't import `meter` — cycle) with a parity test asserting they equal the reader's.
  IAM grants `dynamodb:PutItem` on the budget table only. A pre-merge security review
  caught and fixed a real bypass: a **NaN budget** would pass `usd < 0` and then the
  chokepoint's `spend > budget` (both False), silently disabling enforcement —
  non-finite amounts are now rejected; `..` scope segments too.
- **Deploy-time Price List fetcher — authoritative Bedrock rates (#90, follows #88).**
  #88 fixed the key-mismatch bug with best-effort hand-entered rates; this bakes in
  AUTHORITATIVE numbers from the AWS Price List API at **deploy time** (never on the
  request hot path — NO CLOCKS). `cost/pricelist.py` adds a **pure parser**
  (`parse_price_list`, no boto3 — unit-tested against a recorded us-east-1 fixture)
  plus a thin live fetcher (`fetch_bedrock_price_list`, the only boto3 surface,
  read-only `pricing:GetProducts`) and a CLI: `python -m cost.pricelist --out
  cost/model_rates.json`. The meter/chokepoint load that baked artifact automatically
  (it ships inside the bundled `cost` package; `AGATE_MODEL_RATES_PATH` overrides) via
  `pricing.load_baked_rates` — a plain file read, no env var or API call required.
  A **curated alias map** (`BEDROCK_ALIASES`) maps each `entitlements.TIER_MODELS` id
  to its Price List row, because the API keys rows by human `servicename`
  ("Claude Opus 4.1 (Amazon Bedrock Edition)") / `model` slug ("gpt-oss-20b"), not the
  concrete invoke id — a typo'd alias fails loud rather than silently mis-pricing real
  money. Claude rows prefer the `_Global` (cross-region inference-profile) variant,
  matching the `us.`-prefixed ids we invoke. Verified live: 6 of 8 hand-entered #88
  rates were already exact; the two Gemma output rates were corrected from the live
  data (the hard-default fallbacks are now live-verified too).
- **Budget cascade — hierarchical pre-call enforcement (#81, #70 phase 5).** The
  Tier-1 choke point now allows a call only if it fits under the user/tenant budget
  AND under **every ancestor scope node's** budget (school → dept → course/lab),
  rejecting with the breaching node named (402). `cost.evaluate_cascade` (pure;
  reuses `estimate_call_cost`, prices the call once) layers over the existing
  single-budget `evaluate_precall` (refactored to share one per-node rule — behaviour
  unchanged). The choke point reads each ancestor's budget + running spend
  (`tenant#scope#<node>#period` rows via `meter.scope_pk`) and, on allow, increments
  those scope rows with the call's actual cost. Fail-closed: an unconfined session
  (no `agate:scope`) keeps today's user/tenant gate exactly; a node with no budget
  row imposes no cap. **Tier-1 only** — Tier 0 stays soft/observed (wiring the
  dormant soft cap into the broker is a separate issue). The async log meter is
  unchanged (keys tenant/user); the choke point owns the scope rows, so no double
  count. Budget-row *authoring* to DynamoDB is deferred (#87); displayed/enforced
  dollars use placeholder rates until live pricing is wired (#88).
- **Hierarchical scope reaches the credential boundary for S3 documents (#80, #70
  phase 4).** A session may now carry an `agate:scope` IAM principal tag; the
  generated `data_scope_policy` confines its S3 *document* reads to
  `{tenant}/{scope}/` (strict containment — tenant-root and sibling subtrees denied).
  The confinement is two `Null:false`-guarded Denies, so an **unscoped** session is
  unaffected (tenant-wide, no regression). `agate.tags` gains a `scope` field +
  `_normalise_data_scope` (single path; multi-subtree or garbled → tenant-wide,
  fail-closed; from verified claims only). Proven by live `iam:SimulateCustomPolicy`
  tests (subtree allowed, sibling/root/cross-tenant denied, unscoped still tenant-wide).
  - Also split the S3 Allow into `GetOwnTenantDocs` (gated by resource ARN — fixes a
    latent issue where `GetObject` was wrongly conditioned on `s3:prefix`, which is
    only populated for `ListBucket`) and `ListOwnTenantDocs` (gated by prefix).
  - **Vectors are NOT scope-confined** — the index is per-tenant and scope is row
    metadata IAM can't see; vector subtree enforcement is its own issue (#84). The
    tenant boundary remains IAM-enforced for vectors as before.

### Fixed
- **CloudTrail trail now deploys reliably; re-enabled by default (#75).** The
  `agate-audit` forensic trail intermittently failed to create with "Incorrect S3
  bucket policy" — the L2 `cloudtrail.Trail` construct mutates the bucket policy
  *after* the bucket exists, so CloudTrail's create-time validation could race a
  transiently-incomplete policy (an explicit `DependsOn` didn't help, since it
  depended on a policy the construct was still editing). Fixed by (1) authoring the
  complete CloudTrail bucket policy ourselves as one settled resource — both
  `AWSCloudTrailAclCheck` + `AWSCloudTrailWrite`, scoped to the trail via
  `aws:SourceArn` (deterministic ARN, no cycle) — and (2) switching to the L1
  `CfnTrail`, which does **not** touch the bucket policy, with a `DependsOn` on that
  policy. Verified live (trail `CREATE_COMPLETE`, `IsLogging=true`, no delivery
  error), so it is **on by default** again; opt out with `-c cloudtrail=false` for a
  spend-only deploy. The forensic trail stays independent of the spend path.
- **Per-model pricing — every model was metered at the cheapest (oss) rate (#88).**
  `_DEFAULT_MODEL_RATES` was keyed only by logical tier (`oss`/`mid`/`frontier`), but
  the meter and choke point pass the concrete Bedrock model id
  (`us.anthropic.claude-opus-4-1-…`), which matched nothing and fell through to the
  oss rate — so a frontier Opus call was metered at ~$0.10/$0.40 instead of ~$15/$75,
  making spend, the admin console, and the #81 budget cascade materially wrong.
  `cost/pricing.py` now carries best-effort published list rates for each concrete id
  in `entitlements.TIER_MODELS`, and `llm_rate(model_id, fallback_tier=…)` resolves
  config → per-id default → the id's *tier* default → oss, so even an unlisted id
  prices at its tier rather than oss. The meter and choke point pass
  `entitlements.tier_for_model(model_id)` as that fallback. Rates are **approximate,
  not authoritative** (no live fetch — NO CLOCKS); a deploy-time Price List fetcher
  (#90) will bake in real numbers. Historical spend rows are **not** retroactively
  repriced; new calls price correctly. Config overrides still win.
- **Spend attribution is now unforgeable (#79).** The broker encodes the tenant into
  the STS RoleSessionName as `<tenant>@<subject>` (`agate.tags.role_session_name`), so
  it appears in the assumed-role ARN of every Bedrock invocation-log line. The spend
  meter recovers tenant + user from that ARN (`meter/parse.py`) instead of trusting
  the client-supplied `requestMetadata['agate:tenant']` — which a Tier 0 caller could
  set freely, enabling spend misattribution / soft-cap evasion. `requestMetadata` is
  now only a last-resort fallback for legacy/un-encoded sessions, and spend-key parts
  are sanitised so a `#` can't split the `tenant#user#period` key and silently drop a
  row. Closes the one finding from the consolidation review (#38).

### Security
- Consolidation security re-review of the Phase 9 / #70 session work (adversarial
  pass over the broker→scoped-STS path, the ABAC tag scheme, the admin gate, scope
  retrieval, reasoning patterns, and the new IAM grants; tracked in #38). Boundary
  posture, confirmed:
  - **The ABAC `agate:tenant` session tag is the data fence.** `requestMetadata`,
    `admin_scope`, `role`, and the course/scope retrieval filters are **not** security
    boundaries — they are attribution hints or within-tenant relevance narrowing.
  - `data_scope_policy` (IAM) gates data on `agate:tenant` (+ `agate:tier` for models)
    **only**. `admin_scope` is app-level (console analytics scope); `role` is emitted
    as a session tag but no IAM policy conditions on it (it gates the admin surface,
    not data). Promoting scope to an IAM principal tag for data access is a separate,
    review-gated phase (#80).
  - Reasoning patterns cannot escape a session's entitled model set (compiled against
    `models_for_tier(verified_tier)`; `dispatch` re-checks `allowed_models`).
  - One finding (#79): the spend meter trusts a client-supplied `requestMetadata`
    tenant, so spend can be misattributed — a metering-integrity / soft-cap-evasion
    issue (not an access breach). Remediation deferred to #79/#81 (derive the metered
    tenant from the assumed-role session, not client input).

### Fixed
- Spend attribution (#77): Bedrock calls now pass `requestMetadata`
  (`agate:tenant` + user/affiliation) so the invocation log carries the tenant the
  authoritative-spend meter reads — previously every spend row keyed to `unknown`
  because nothing set it. Wired in the agent backend (`BedrockBackend`, from the
  verified token) and the Tier 0 web transport (`bedrock.ts`, from the session
  scope), both sanitised to Bedrock's metadata grammar. It's an attribution hint,
  not a security boundary — the credential's ABAC tenant tag remains the fence, and
  the meter still treats a missing value as `unknown`.

### Changed
- `agate-audit`: the CloudTrail management-plane Trail is now **opt-in**
  (`-c cloudtrail=true`, default off). Its create-time bucket-policy validation is
  flaky against a fresh bucket even with the correct policy + an explicit dependency
  (#75). The forensic trail is independent of the authoritative-spend path (spend
  table + Bedrock invocation logging + meter), so gating it lets the spend path —
  what the governed-access console needs — deploy cleanly. Found deploying live.

### Added
- **Hierarchical scope — admin RBAC slice** (#70, phase 3, app-level). A *scoped*
  admin (a dean/chair) now sees only their own tenant's analytics in the console,
  while a tenant-wide admin sees all. `claims_to_tags` derives `admin_scope` (the
  subtree node[s] a scoped admin governs) from an `admin_scope`/`scope` claim —
  **fail-closed**: a non-admin never gets a scope (a forged claim on a member is
  inert), and an admin with no scope is tenant-wide. The console API restricts the
  payload to the admin's tenant when scoped. The demo pool gained a `custom:admin_scope`
  attribute + pre-token mapping.
  - **Security boundary held:** `admin_scope` is APP-LEVEL only — it is NOT emitted
    as an STS session tag and does NOT touch `data_scope_policy` / IAM, so tenant
    isolation is byte-for-byte unchanged (asserted in tests). Promoting scope to an
    IAM principal tag for *data* access, and subtree-granular spend (budget cascade),
    remain the separate, review-gated phases of #70.
- **Hierarchical scope — retrieval slice** (#70, phase 2). RAG now supports a
  `school/department/course` (teaching) or `school/department/lab-or-project`
  (research) scope tree, giving **subtree visibility**: a dean sees their whole
  school, a chair their department, a student only their course. A document under
  `{tenant}/{scope-path}/…` stores its **ancestor-path list** (`scope_ancestors`);
  retrieval matches the session's scope node(s) with `$in` (S3 Vectors has no prefix
  operator — validated live, so the ancestor-list encoding is the mechanism). Pure
  `agate.rag.scope_path_from_s3_key` / `ancestors` / `scope_filter`, mirrored by the
  web retriever's `scopeFilter`; backward-compatible (a flat course is a one-segment
  scope, and old `course`-tagged docs still match). Verified live across dean / chair
  / student / sibling-dept / sibling-course / no-scope. **Deliberately does NOT touch
  the ABAC session tag or IAM** — this narrows within the tenant index the credential
  already gates; the RBAC + budget-cascade phases of #70 are separate, review-gated.
- **Composable reasoning patterns — live** (Phase 9 Track 2, #64). The "do better"
  axis: reasoning constructs are now institution-composed declarative configs over
  the existing Panel/Analyze primitives, not hard-coded modes — the thing neither
  NebulaONE (chat + prebuilt agents) nor Amazon Quick (task-agents) offers.
  - `agate/patterns.py` (pure): a `Pattern` names *roles* (label + system prompt +
    a model PREFERENCE — cheapest/balanced/best, never a concrete model id) over a
    mode. `compile_pattern` materialises it against the verified caller's ENTITLED
    models into an ordinary dispatch payload, so `agate.agent_dispatch` runs it
    unchanged and the entitlement check still holds. A reviewed registry (no DSL, no
    end-user builder — the deliberate Phase-9 scope). Two reference patterns:
    `lit-review` (claims/methods/gaps → cited synthesis) and `red-team`
    (steel-man for/against → verdict).
  - The panel orchestrator now honours a **per-role `system`** prompt (the recipe),
    falling back to the shared review prompt — so each pane reasons in its own role.
  - The agent server runs a `{pattern: key}` payload (compile → dispatch); the SPA
    offers the patterns in the mode picker under "Reasoning patterns". Unit-tested
    + verified live (`red-team` returned for/against panes + reconciliation, using
    the caller's entitled models).
- **Per-tenant + per-course RAG — live** (Phase 9 Track 3, #65). The data plane
  (`agate-data`) is deployed and proven against real S3 Vectors: upload to
  `{tenant}/...` → ingest Lambda → Titan embeddings (1024-dim) → per-tenant S3
  Vectors index → scoped query. **RAG uses S3 Vectors directly, not a Bedrock
  Knowledge Base** — a KB needed a clock-bearing vector store, and direct S3 Vectors
  keeps NO CLOCKS *and* the scoped-STS credential as the isolation fence.
  - **Per-course scoping** (the unique asset): a document under `{tenant}/{course}/…`
    is tagged with that course; retrieval filters to the session's `agate:courses`
    (+ tenant-wide docs). A course corpus is therefore visible only to enrolled
    students, derived from the verified claim — fail-closed (no enrollment → no
    course docs). `agate.rag.course_from_s3_key`/`course_filter` (pure) + the web
    retriever's `courseFilter`. Verified live: a chem-101 session sees only chem-101
    material, never bio-200; an unenrolled session sees neither.
  - Flat course model for now; the hierarchical school/dept/course (+ lab/project)
    scope for RBAC + budgets is designed in #70.
- **Governed-access console — live** (Phase 9 Track 1, #63 — second slice). The
  admin spend-analytics dashboard end-to-end:
  - `infra/stacks/admin.py` (`agate-admin`): the admin Lambda behind its own API
    Gateway HTTP API, with a read-only grant on the spend table (by ARN, so no hard
    cross-stack dependency on `agate-audit`). OIDC config from the same context keys
    as the broker. Per-request, no clock.
  - The admin Lambda degrades to **empty analytics** (200) when the spend table
    isn't deployed yet, rather than erroring — the console is useful before audit.
  - `web/src/admin/view.ts`: the dashboard view (total spend, per-tenant table with
    scoped headers, top spenders) in the design system; an **Admin · Usage** entry
    in the pop-out nav (shown when `VITE_ADMIN_URL` is set; the API's 403 is the real
    gate). `agate-demo-idp` gained a `custom:role` attribute + pre-token mapping so a
    demo admin user issues an `agate:role=admin` token. Unit-tested.
- **Governed-access foundations** (Phase 9 Track 1, #63 — first slice). The
  differentiator vs NebulaONE ("usage limits per user") and Amazon Quick (no
  per-capita entitlement): admin is gated at the *credential* boundary, not the app.
  - `agate:role` is now a fifth ABAC session tag, derived in `claims_to_tags` from a
    `role`/`isAdmin` claim — **fail-closed**: only an explicit recognised admin claim
    yields `admin`; anything missing/garbled is `member`. It gates the console only,
    not model/data access.
  - `agate/admin.py`: pure, AWS-free spend analytics — per-tenant rollups, top
    spenders, and a console payload derived from the authoritative spend-table rows
    (never trusts a stored total it can't re-derive). Unit-tested.
  - `infra/functions/admin/handler.py`: the console API — verifies the IdP token,
    requires the verified `agate:role == admin` (else 403, no data), then returns the
    analytics. Read-only this slice; per-request, no clock.
  - Shared app chrome (`web/src/chrome/nav.ts`): a top bar with a hamburger toggle
    and an accessible **pop-out side-navigation drawer** (labelled dialog, Esc to
    close, focus management, scrim) — used by the main SPA now and the admin console
    next. Wired into the main SPA. Unit-tested.
- **SPA design system + accessibility baseline** (Phase 9 Track 0, #62). Extracted a
  dark, dense visual language into `web/src/styles/agate.css` (CSS custom-property
  tokens, self-hosted **Atkinson Hyperlegible** via `@fontsource`, a CSS-grid
  header/main/sidebar shell with a running-cost meter). The existing Ask/Panel/Analyze
  UI is retrofitted into it and made **accessible by construction** so later tracks
  inherit it: a skip link, semantic landmarks (`header`/`main`/`aside`), labelled
  controls, an `aria-live` answer region with `aria-busy` during a run, `role="alert"`
  errors, `role="group"`/labelled model panes, a labelled Analyze code cell, a
  `:focus-visible` ring, and `prefers-reduced-motion` support. Pure front-end, no infra.
- **Agent path (Panel/Analyze) is live end-to-end.** The reference agent container
  is built (linux/arm64, as AgentCore requires) and pushed to ECR, and `agate-agent`
  deploys the AgentCore Runtime + Code Interpreter against it. `agate-identity` now
  grants the authenticated session role `bedrock-agentcore:InvokeAgentRuntime` on the
  agate agent runtimes (bounded by a matching permissions-boundary ceiling), so the
  SPA's SigV4-signed call (with broker-vended scoped creds) reaches the Runtime; the
  container still re-derives the caller's tier from the verified JWT (SEC-4b). The
  SPA's Panel/Analyze modes light up once `VITE_AGENT_RUNTIME_ARN` is set.
- `agent/server.py` `_resolve_models()`: when an invocation omits the roster /
  generator / router (the SPA sends only `{question, idp_token, mode}`), the
  container materialises concrete **entitled** Bedrock model ids from the verified
  tier — so a bare payload never sends a logical label like `oss` as a `modelId`
  (which Bedrock rejects). Caller-supplied config is left untouched. Unit-tested.
- **Click-to-demo login via Cognito Hosted UI** (`web/src/auth/login.ts`). The SPA
  now shows a *Log in* / *Log out* button and gates chat on auth: an unauthenticated
  visitor is redirected to the demo pool's Hosted UI (OIDC implicit flow), comes back
  with an `id_token` in the URL fragment, and the SPA captures it into sessionStorage
  and scrubs it from the URL. The broker verifies it server-side exactly as a campus
  token. Falls back to a manual `#idp_token=` paste when `VITE_COGNITO_DOMAIN` is
  unset. Config via `VITE_COGNITO_DOMAIN` / `VITE_COGNITO_CLIENT_ID`. `agate-demo-idp`
  registers the SPA origin as the client's callback/logout URL (`-c site_url=…`,
  localhost included for `vite dev`) and outputs `HostedUiDomain`. Pure token/URL
  logic is unit-tested.
- `agate-identity` now exposes the broker over an **API Gateway HTTP API** (CORS,
  no API-level auth — the broker authenticates from the verified JWT, not an AWS
  principal), output as `BrokerUrl`, so the browser SPA can reach it. Per-request,
  no idle endpoint fee (NO CLOCKS). The broker's OIDC verification config
  (`AGATE_OIDC_ISSUER`/`_JWKS_URL`/`_AUDIENCE`) is read from CDK context
  (`-c oidc_issuer=… -c oidc_jwks_url=… -c oidc_audience=…`) instead of a
  post-deploy CLI patch — the same keys take a campus IdP or the demo pool's
  outputs, so the demo is reproducible from `cdk deploy` alone.
- Optional **source-IP fence** on the broker: set `-c allow_ip=1.2.3.4` (or a CIDR,
  or a comma-separated list) and the broker denies any request whose API-Gateway
  source IP is outside the allowlist (`AGATE_IP_ALLOWLIST`). HTTP APIs have no
  resource policy, so the broker enforces it in-handler; it fails closed on a blank
  source IP or a malformed allowlist. Empty = open (JWT auth only). Unit-tested.

### Changed
- `agate-demo-idp` SPA client now enables username/password auth flows
  (`user_password` + `admin_user_password`) in CDK, so a demo operator/script can
  mint a token without the hosted-UI redirect. Demo-only convenience, codified
  rather than applied by hand post-deploy.

### Fixed
- `agate-data` deploy failed creating the S3 Vectors indexes (403 "Insufficient
  access to perform asynchronous indexing"): the per-tenant KMS CMK didn't grant
  the S3 Vectors indexing service principal (`indexing.s3vectors.amazonaws.com`)
  permission to use the key. Added a `SourceAccount`-scoped key policy statement
  (`kms:Decrypt`/`GenerateDataKey`/`DescribeKey`). Found deploying the data plane live.
- Analyze mode failed live (HTTP 500): the agent Runtime execution role could
  invoke Bedrock but not the **Code Interpreter** it runs generated code in
  (Ask/Panel never call it, so they worked). Added
  `bedrock-agentcore:InvokeCodeInterpreter` (+ Start/Stop/Get session) scoped to the
  agate code interpreters. Analyze now returns the full `route → code → answer →
  receipt` stream with both codegen and execution cost rows, verified live.
- AgentCore endpoint version pinning: the `default` `CfnRuntimeEndpoint` is now
  bound to the Runtime's current `AgentRuntimeVersion` (`Fn::GetAtt`), so a deploy
  that bumps the image also rolls the endpoint to the new version. Previously the
  endpoint kept serving the prior version until repointed by hand (symptom: a stale
  container, or HTTP 424 when the new image differed).
- Agent container Dockerfile was missing `COPY cost/` — `agent/server.py` imports
  `cost.CostMeter`, so the Runtime crashed at startup with `ModuleNotFoundError:
  No module named 'cost'`. Now copies the `cost` package alongside `agate`/`agent`.
- `agate-agent` deploy hardening (all found deploying live): the Runtime execution
  role now carries the **ECR pull** permissions AgentCore validates at create time
  (`ecr:GetAuthorizationToken` + scoped `BatchGetImage`/`GetDownloadUrlForLayer`)
  and **CloudWatch Logs** write on the agent log group; the Runtime + Code
  Interpreter now `DependsOn` the role's inline policy so creation doesn't race the
  policy attach (which previously failed "Access denied while validating ECR URI").
- IAM role descriptions can't contain non-ASCII: an em-dash in the agent runtime
  role description failed role creation. Normalised em-dashes to `-` in all stack
  resource descriptions.
- Broker endpoint moved from a Lambda **Function URL** to an **API Gateway HTTP
  API**. Public (`AuthType NONE`) Function URLs are blocked at the edge by an
  account/org guardrail (Lambda Block Public Access) in some environments — they
  return a 403 "Forbidden" before the handler runs, even with a correct public
  resource policy. An HTTP API invokes the broker via the service principal (IAM),
  so it is unaffected, and it's the more idiomatic browser-facing front door. Same
  per-request / no-idle-cost posture.
- Lambda asset bundling: the Docker-free local bundler now fetches **Linux/x86_64**
  wheels for the Lambda runtime instead of host-platform wheels. `pyjwt[crypto]`
  pulls `cryptography` (a native extension); on a macOS/arm64 dev box the local
  bundler was installing a macOS wheel, so the deployed broker crashed at import
  with `invalid ELF header` (`Runtime.ImportModuleError`). `pip_bundled_code()` now
  passes `--platform manylinux2014_x86_64 --implementation cp --python-version 3.13
  --only-binary=:all:`. Caught deploying the Tier 0 identity stack live; the broker
  now verifies tokens and vends scoped STS end-to-end.

### Changed
- **Renamed the project `agg` → `agate`** (named for agate, a banded form of bedrock —
  ties to Amazon Bedrock). The rename is now complete across both the code identifiers
  and the distribution identity: the CLI binary, the `agate:` ABAC session-tag namespace
  (`agate:tenant`/`tier`/…), all `agate-*` AWS resource and CDK stack names
  (`agate-identity`, `agate-data`, …), the `AGATE_*` Lambda env vars, the `agate/` Python
  package, the package slug (`pyproject`/`package.json` → `agate`), the Go module path
  (`github.com/scttfrdmn/agate/cli`), the `docs/agate-*` filenames, and the GitHub repo.
  The name remains provisional. **Operational note:** because resource and stack names
  changed, an existing `agate-*` deployment is not upgraded in place — destroy the old
  stacks and deploy the `agate-*` ones (nothing was live).

### Added
- Demo readiness — `infra/stacks/demo_idp.py` (`agate-demo-idp`): an optional,
  throwaway Cognito User Pool that issues real RS256 JWTs so the gateway can be
  demoed without a campus IdP. A pre-token-generation Lambda
  (`infra/functions/demo_idp/pretoken.py`) maps the demo user's
  `custom:affiliation|tenant|courses|grant` attributes onto the top-level `agate`
  claim names, so the demo token verifies (SEC-4) and scopes (ABAC) exactly like a
  campus token with no gateway changes. The stack outputs the OIDC issuer, JWKS URL,
  and audience to wire into the broker/agent `AGATE_OIDC_*` config. Production omits
  this stack and points the broker at the real IdP.
- Demo readiness — the SPA now drives the full academic interaction model (#39):
  `web/src/main.ts` adds a mode selector (Ask / Panel / Analyze) and routes each
  mode — Ask streams Tier 0 browser-direct; Panel and Analyze invoke the AgentCore
  agent and render the multi-pane layout, the side-by-side divergence view, the
  notebook Analyze cell, and a live cost receipt from the run event stream. The
  agent invocation carries the IdP token (verified server-side; the SPA never sends
  a tier).
- Demo readiness — `infra/stacks/web.py` (design §11, #40): the static SPA on a
  private S3 bucket behind CloudFront with Origin Access Control (no public bucket,
  no fixed CloudFront fee → NO CLOCKS), SPA deep-link error mapping, and a
  `BucketDeployment` that publishes `web/dist` when present. Outputs the site URL.

### Security
- SEC-2b: the agent execution role no longer holds S3 Vectors / S3 read permissions
  — the agent does not retrieve (evidence is supplied in the invocation payload, the
  SPA having run the tenant-scoped query Tier-0-style). Removing the unused grant
  closes the latent cross-tenant data read the review flagged; a future retrieval
  tool must derive the tenant from the verified token before any grant is re-added.
- **SEC-4 — real JWT verification replaces the Phase-1 placeholder across all entry
  points.** A re-review of the SEC-1/2 fixes found they had relocated trust to inputs
  whose trustworthiness wasn't established: the chokepoint reused the broker's
  unsigned-token placeholder behind a live Function URL (SEC-4a), and the agent
  derived its tier from an unsourced `X-Agg-Verified-Tier` header (SEC-4b).
  - `agate/jwt_verify.py`: one shared real verifier — RS256 against the IdP JWKS,
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
    looked up server-side from a new `agate-budget` table keyed by the verified
    identity, and input tokens are always estimated server-side. The body carries
    only `idp_token` + `model`/`messages`/`max_tokens`.
  - **SEC-2 — agent path had no tenant/tier enforcement (`agate/agent_dispatch.py`,
    `agent/server.py`, `infra/stacks/agent.py`).** The container invoked any model
    the payload named, and the Runtime execution role granted Bedrock + S3 Vectors on
    `Resource:*`. Dispatch now rejects any model outside the verified caller's tier
    (`allowed_models`, from the inbound-JWT `agate:tier`, fail-closed to oss); the
    execution role is scoped to agate's entitled model ARNs and this deployment's
    vector/docs bucket ARNs.
  - **SEC-3 — LTI tenant fallback (`agate/lti.py`).** A registration without a `tenant`
    fell back to the LTI context claim (instructor-controlled), enabling cross-tenant
    access on a shared LMS. The fallback is removed; a missing registration tenant
    now fails closed (`LtiClaimError`).

### Changed
- Refactor pass (no behavior change; demo-readiness #35–#37): removed three
  duplications surfaced now that the build is complete.
  - `agate/contracts.py` defines the `Backend` / `CostMeter` Protocols + `Emit` /
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
    `agate.entitlements` table that drives the IAM model-access policy — a per-tier
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
    identity + `agate:tenant` tag, derives the `{tenant}#{user}#{period}` (+ rollup)
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
  Go module for the `agate` CLI (`cli/`), Vite + TypeScript SPA skeleton (`web/`), and the
  component directories from design §11 (`policy/`, `cost/`, `meter/`, `lti/`, `agent/`,
  `ingest/`, `docs/`). `README.md`, this changelog, `.gitignore`, and `cdk bootstrap` notes.
- Phase 1 — identity broker + ABAC:
  - Pure, side-effect-free `claims_to_tags()` translation (the `agate:` session-tag scheme,
    §13.1) with full unit-test coverage and no AWS dependency.
  - Single-source-of-truth tier → entitled-model-ARN table, shared by the broker and the
    generated IAM model-access policy.
  - `infra/stacks/identity.py`: Cognito **Identity Pool** (federated SAML/OIDC, no User
    Pool), the authenticated role + permissions boundary keyed on `agate:` principal tags,
    and the per-request **broker Lambda** that validates the IdP token, derives the four
    tags, and vends scoped STS credentials.
  - Phase 1 end-to-end proof: IAM policy simulation asserting `Converse` is allowed for an
    entitled model ARN and denied for a non-entitled one, scoped purely by `agate:` tags.
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
    an opt-in live `ConverseStream` smoke test (`AGATE_LIVE_SMOKE=1`), verified against
    Bedrock.
- Phase 3 — data plane (S3 Vectors RAG):
  - `infra/stacks/data.py`: an `agate-docs` S3 bucket (per-tenant prefix, versioned,
    retained), an S3 Vectors vector bucket with one **index per tenant** (1024-dim,
    cosine), and a **per-tenant KMS CMK** on each index. Each index is tagged with its
    `agate:tenant` so the Phase 1 ABAC data-scope policy isolates reads. Built on L1
    `Cfn*` constructs (no L2 for S3 Vectors yet).
  - `ingest/handler.py`: embed-on-upload Lambda — S3 `ObjectCreated` → chunk → Bedrock
    Titan embeddings → `PutVectors` into the tenant's index. Tenant is derived from the
    key prefix and fails closed; one bad object never aborts the batch.
  - `agate/rag.py`: pure, AWS-free chunking, tenant-key derivation, and vector-record
    assembly with full unit-test coverage.
  - `web/src/rag/`: query-embed → scoped `QueryVectors` on the tenant index → context
    injection; a pure, unit-tested context builder. RAG is opt-in via an optional
    `ContextProvider` on `ChatSession` (grounding is sent per turn, never persisted).
  - Tenant-isolation proof (IAM policy simulation): a `chem`-scoped session may
    `QueryVectors` its own index and is **denied** the `psych` index (both directions).
- Phase 4 — LTI 1.3 tool provider:
  - `agate/lti.py`: pure, AWS-free mapping of an LTI 1.3 launch (roles, context, NRPS)
    into the claims dict that the Phase 1 `claims_to_tags()` consumes — so LTI is one
    concrete source of `agate:affiliation` / `agate:courses`, with no second tag scheme.
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
  - `agate/panel/schema.py`: the `Divergence` Pydantic model mirroring the §10.2.5
    draft-07 schema (forbids extra properties, requires ≥1 position per claim,
    constrains the stance/kind enums) plus `strip_fences()` for accidental Markdown
    fences around the adjudicator's JSON.
  - `agate/panel/prompts.py`: the `ADJUDICATE_SYSTEM` prompt (structured-only output)
    and a default review prompt; reviewer labels are roster config, kept neutral.
  - `agate/panel/orchestrator.py`: `run_panel` — N roster members review the same
    evidence in parallel over injected `Backend`/`CostMeter` interfaces (no AWS in
    core), each emitting its own `model` start/done + per-pane `cost`; the
    adjudication tail validates the structured output and emits a `divergence`
    event, falling back to an unstructured `answer` on malformed/invalid output.
  - Tests (fakes only, no AWS): per-pane events, identical evidence to every
    member, a well-formed adjudication whose `pane` values are a subset of the
    roster labels, cost accumulation, and the malformed- **and** schema-invalid-
    adjudicator fallback paths.
- Academic interaction model — Phase 3 (Analyze cell / Code Interpreter):
  - `agate/analyze/schema.py`: pure, AWS-free `extract_code()` (pulls the Python
    script from the model's fenced output), `parse_invoke_result()` (normalises an
    AgentCore `InvokeCodeInterpreter` stream result into text/image content blocks),
    and `result_to_events()` (maps a result to `answer`/`chart` events, surfacing a
    traceback on error rather than swallowing it).
  - `agate/analyze/prompts.py`: the `ANALYZE_SYSTEM` prompt (one self-contained,
    network-free Python script; chart saved to a known path for inline rendering).
  - `agate/analyze/orchestrator.py`: `run_analyze` over injected `Backend` /
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
  - `agate/multimodal.py`: pure, AWS-free helpers — the Nova embedding request/response
    mapping, ingestion-path selection (native multimodal vs parser+text fallback),
    and visual-citation resolution (`citation` event + figure/table corpus deep links).
  - `infra/stacks/data.py`: a **3072-dim multimodal S3 Vectors index per tenant**,
    built alongside (not replacing) the 1024-dim text index, sharing the tenant CMK
    and `agate:tenant` tag; a `_mm-artifacts/` supplemental-storage prefix. Embedding
    dimension is now per-index config, not a global constant.
  - `web/src/rag/multimodal.ts` + `citation.ts`: query-by-image and figure-aware
    text retrieval against the multimodal index, and visual-citation/deep-link
    resolution mirroring the Python helpers.
  - Tests (fakes only, no AWS) across Python and TypeScript: embedding request
    shapes, path selection, the 3072-vs-1024 dimension guard, and citation/deep-link
    resolution for text vs figure vs table.
- Academic interaction model — Phase 5 (reproducible run artifact):
  - `agate/artifact.py`: pure, AWS-free `RunArtifact` Pydantic model + `build_artifact()`
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
  - `agate/router.py`: pure `classify_mode()` (a router model's one-word reply →
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
  - `agate/agent_dispatch.py`: pure, AWS-free `dispatch()` that resolves the mode
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
- Phase 7 — the `agate` admin CLI (Go): real `tenant`, `budget`, `deploy`, and
  `ingest` commands replacing the Phase 0 stubs.
  - `internal/config`: a pure `.agate.json` model — tenant set (validated against the
    `agate:tenant` charset, kept sorted/deduped) and per-tenant budgets (which the
    soft cap reads). Load treats a missing file as empty; full table tests.
  - `internal/commands`: pure plan construction — `deploy` turns the tenant set into
    `cdk deploy ... -c tenants=...`; `ingest` targets the FERPA-correct
    `s3://agate-docs-…/{tenant}/<file>` prefix. Both return the exact argv, tested.
  - The cloud-mutating commands (`deploy`, `ingest`) **plan by default and run only
    with `--confirm`** — agate never changes cloud state implicitly. `budget set`
    accepts the natural `set <tenant> --usd N` ordering.
  - `gofmt`/`go vet` clean; `go test ./...` green across the three packages.

### Fixed
- **Phase 1 proven live** (first real AWS deploy, us-east-1): the identity stack
  deploys and the full chain works against real Bedrock — broker Lambda derives the
  `agate:` tags, vends scoped STS creds, and IAM/ABAC allows an entitled model and
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

[Unreleased]: https://github.com/scttfrdmn/agate/commits/main
