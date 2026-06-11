# aws-genai-gateway
**AWS-Native GenAI Gateway for Higher Education**  ·  CLI: `agg`
## Design & Implementation Plan (for Claude Code)

> **Name (provisional).** Project / repo / package: **`aws-genai-gateway`**. CLI binary and short
> handle used throughout: **`agg`** (session-tag namespace `agg:`, bucket prefix `agg-docs`). Chosen
> "for now" — still swappable via a global rename; nothing in the design depends on it.

---

## 0. Thesis (read this first)

This is **not a platform.** It is a small thing that lives *inside a perimeter the
institution already owns and pays for* (campus network, Internet2 uplink, central
egress). A person federates their campus identity, receives a key scoped to exactly
the models and documents they're entitled to, and the browser talks to both — with
the talking and the documents fenced by the *same* boundary.

Everything nebulaONE (and the AWS solution templates) frame as a "platform" is the
thing that generates standing cost. Strip the platform framing and what remains is
small enough that the cost question answers itself.

**The product is four parts:** a static frontend, a credential broker, a couple of
cheap data stores, and Bedrock. A thin policy/routing component is *optional* and
added only under a named requirement.

---

## 1. Governing principle: **NO CLOCKS**

> Nothing in this system is allowed to bill by the wall-clock hour.
> Every component bills **per-request** or **per-byte-stored**, or it does not ship.

This single rule kills the entire class of standing cost. It is the review test:
if a proposed resource bills while nobody is using it, it is rejected unless a
specific institutional requirement drags it back in *and* that requirement is worth
the rent. "Production-grade" must **earn** every hourly dollar against a named need;
it is not assumed.

Corollary design tension, stated once so the implementer internalizes it:

> Every governance feature expressible as an **IAM or Cedar policy**, or pushed onto
> a **Bedrock-native feature** (invocation logging, Guardrails, application inference
> profiles for cost tags), stays at the **edge** and stays **free**. Every governance
> feature you insist on **centralizing** pulls the choke point inward and starts
> costing idle. Keep as much as possible on the policy side of that line.

---

## 2. Architecture — the tiers

The frontend is a constant. The backend it talks to is swappable across three tiers.
Default to Tier 0. Move inward only when forced.

```
                         ┌─────────────────────────────────────────────┐
   Campus IdP            │  Tier 0 — FLOOR (default, zero standing cost)│
  (Shibboleth/           │                                              │
   InCommon, Entra,      │   Static SPA  ──►  Bedrock Converse (direct) │
   Okta)                 │   (S3+CF)          + S3 Vectors (direct)     │
        │                │        ▲                                     │
        │ SAML/OIDC      │        │ scoped, short-lived STS creds       │
        ▼                │        │                                     │
  Cognito Identity Pool ─┴────────┘                                     │
        │  (federates IdP, vends scoped temp creds via session tags)    │
        └───────────────────────────────────────────────────────────────┘

   Tier 1 — THIN CHOKE POINT (add only for: hard pre-spend budget cutoffs,
            centralized guardrail/PII inspection, or non-Bedrock model routing)
        Static SPA ──► Lambda (Function URL, response streaming) ──► Bedrock / others
                       + DynamoDB budget counter + Guardrail

   Tier 2 — LAST RESORT (only if you specifically want LiteLLM's batteries:
            multi-provider admin UI, semantic cache — and accept one always-on box)
        Static SPA ──► LiteLLM on Fargate/EC2 ──► providers

   AGENT PATH — for agentic workloads (multi-step, tools, code, multi-model
            orchestration). Serverless, scales to zero, I/O-wait free.
        Static SPA ──► AgentCore Runtime (microVM) ──► Bedrock models
                       ├─ Gateway (APIs/Lambdas/MCP as tools)
                       ├─ Policy (Cedar authz on tools/actions)   ← replaces clAWS
                       ├─ Code Interpreter (sandboxed exec)
                       ├─ Memory (short/long-term agent state)
                       └─ Identity (inbound = user; outbound = tool creds)
```

**Key property:** the SPA exposes a single client-side *transport* interface with three
implementations — a **Bedrock-SDK adapter** (Tier 0 chat, SigV4 with Cognito creds), an
**OpenAI-fetch adapter** (Tiers 1/2 chat, hits a Function URL / LiteLLM), and an
**AgentCore adapter** (agent path, invokes an AgentCore Runtime agent). Switching is a
config change, not a rewrite. Build Tier 0 first; the rest are opt-in.

**Chat vs agent — the reasonable AgentCore boundary:** a single Converse + a vector query
is plain chat; it stays browser-direct (Tier 0) and never pays for a runtime. Anything
genuinely *agentic* — tool use, code execution, multi-model orchestration — runs on
AgentCore, which is serverless and scales to zero so it still honours "no clocks." Don't
bolt AgentCore onto simple chat.

**Why browser-direct-to-Bedrock is safe:** Cognito Identity Pools exist to vend
*scoped, short-lived* STS credentials to client apps. The creds are the user's own,
narrowed by the authenticated IAM role + session tags to exactly their model
entitlement and data scope. A user calling Bedrock outside the SPA with those creds
can only spend their own allocation against their own entitlement — they cannot
exceed their IAM scope. Budget is handled by a **soft cap** (§7.1) — the broker stops
vending model creds once a user is over budget, and short creds TTL bounds the overrun,
so no in-flight cutoff is needed. Tier 1 exists only for institutions that want *exact*
pre-call enforcement, centralized inspection, or non-Bedrock routing.

### 2.5 Model access — the sovereignty ladder

Model breadth is a **Bedrock-native property, not something `agg` assembles.** Any model the
institution can reach answers the **same Converse API**, so the chat path, CostMeter,
Guardrails, KB, and agent plumbing are identical regardless of where a model is hosted. What
changes from rung to rung is only the **cost profile** — and exactly one rung reintroduces a
clock, which `agg` *names and meters* rather than hides.

| Rung | What | Hosting | Clock? |
|---|---|---|---|
| **0 — Managed FM** (default) | Bedrock serverless foundation models (OpenAI, Anthropic, Llama, Mistral, Gemma, Qwen, DeepSeek, gpt-oss, …) | Bedrock-managed | **No** |
| **1 — Custom Model Import** | Your own / fine-tuned weights for a mainstream architecture (Llama, Mistral, and Llama-distilled variants incl. DeepSeek-R1-Distill) | Bedrock-managed, **serverless, scales to zero** | **No** (cold-start on reload) |
| **2 — Bring-your-own endpoint** | Exotic architectures or full model sovereignty; any HF model | **SageMaker endpoint** (direct, or via **Bedrock Marketplace** so it still answers Converse) | **Yes** ⚠️ standing GPU endpoint (~$500+/mo even idle) |

**Climb only as far as the requirement forces.** Rung 0 covers nearly everything; the one
true gap is Google **Gemini** (Vertex-only — Gemma covers the open-weight line instead).
Rung 1 is the "we have a fine-tune" path and costs **zero new `agg` surface**: drop weights in
S3, register, invoke through the identical API, with native Guardrails / KB / Agents support.
Rung 2 is the only place **NO CLOCKS is suspended — by the institution's explicit choice** —
and it is where the meter earns its keep: the moment a session targets a Rung-2 endpoint, the
**CostMeter receipt flags it as a standing endpoint** ("$X/hr whether idle or not"), so the
clock is *visible at point of use* instead of buried in a bill. This is also the natural seam
for an external control plane (e.g. Ephemeron) to manage those endpoints — VRAM-aware
placement, scale-to-zero orchestration — turning "`agg` → SageMaker" into "`agg` → control
plane → SageMaker" later without changing the call path.

> CISO caveat: treat **Grok** (xAI/SpaceXAI) as Rung-0-available-on-request at most — the
> org/endpoint churn and content-safety reputation make it a poor default production
> dependency, regardless of Bedrock availability.

---

## 3. Components

| Component | AWS service | Idle cost | Tier |
|---|---|---|---|
| Static SPA | S3 + CloudFront | ~$0 (no fixed CF fee) | 0 |
| Identity broker | Cognito **Identity Pool** (federated) | $0 | 0 |
| Credential vending | STS (via identity pool role) | $0 | 0 |
| Models | Bedrock, **on-demand only** | $0 | 0 |
| Custom models (optional) | **Bedrock Custom Model Import** (your weights; serverless, scales to zero) | $0 idle | 0/1 |
| Documents | S3 (per-tenant prefix) | storage pennies | 0 |
| Vectors / RAG | **S3 Vectors** (GA) | storage pennies | 0 |
| Ingestion | Bedrock Knowledge Bases *or* embed-on-upload Lambda | per-use | 0 |
| Audit | Bedrock invocation logging + CloudTrail → S3 | storage pennies | 0 |
| Chat history (optional, cross-device) | DynamoDB **on-demand** | $0 | 0 |
| LMS connector | **LTI 1.3** tool provider on Lambda + HTTP API | per-request | 0 |
| Policy plane | **AgentCore Policy** (Cedar authz), Bedrock **Guardrails** | per-use | 0 |
| Agent runtime | **AgentCore Runtime** (serverless microVM; active vCPU/GB, I/O-wait free) | $0 idle | agent |
| Agent tools | **AgentCore Gateway** (APIs/Lambdas/MCP → tools) | per-invocation | agent |
| Code execution | **AgentCore Code Interpreter** (sandbox) | per active-sec | agent |
| Agent memory | **AgentCore Memory** (short/long-term) | per-record | agent |
| Agent identity | **AgentCore Identity** (OAuth vault; Cognito/Okta/Entra) | per-use | agent |
| Agent tracing | **AgentCore Observability** (CloudWatch/OTel) | CloudWatch rates | agent |
| Cost meter | `CostMeter` (lifted from agencore-demo) + invocation-log → `spend` table | per-request | 0 |
| Soft cap | budget read inside the identity broker (at creds vend/refresh) | $0 | 0 |
| Hard pre-call cutoff (optional) | inference via Lambda assuming the user's own role | per-request | 1 |
| Multi-provider/cache (optional) | LiteLLM container | hourly ⚠️ | 2 |
| Visual authoring (optional) | **Bedrock Flows** (serverless visual canvas) | per node-transition | 0 |
| BYO model endpoint (optional) | **SageMaker endpoint** / **Bedrock Marketplace** (answers Converse) | hourly ⚠️ | 2 |

**Idle cost of Tier 0 ≈ the rent on the documents sitting in S3.** There is nothing
with a clock in it.

### 3.1 Load-bearing components (where the real engineering is)

Everything above is assembly **except these two**. Spend the care here:

1. **The claims → scoped-STS broker.** Campus federated identity already exists and is
   wired campus-wide (Shibboleth/InCommon, Entra, Okta) — we **hook into it**, not
   rebuild it. The Cognito Identity Pool consumes the institution's existing IdP; the
   part we own is the *translation*: map their existing claims (eduPerson affiliation,
   enrolled-course list) onto **session tags**, and vend a temp credential = the
   authenticated role *narrowed by those tags*. The care goes into the translation,
   not into identity itself.
2. **The ABAC scheme.** One tag scheme that simultaneously governs (a) which Bedrock
   models the session may invoke and (b) which S3 prefixes / S3 Vectors indexes it may
   read. This is what makes "one boundary governs model and data" true.

If these two are right, the rest is plumbing.

---

## 4. How data connects — identity-scoped retrieval

Data does **not** connect through a pile of connectors. It connects through **identity**.

- Documents live in S3, partitioned by tenant: `s3://agg-docs/{tenant}/...`
  (tenant = college, department, or course).
- Each tenant gets its **own S3 Vectors index**, with its **own customer-managed key**.
  S3 Vectors supports per-index CMK and tag-based ABAC — use both for isolation.
- Ingestion: Bedrock Knowledge Bases pointed at the tenant's S3 prefix + vector index,
  **or** a small embed-on-upload Lambda (S3 event → Bedrock embeddings → S3 Vectors).
- At query time: embed the query → top-k from the tenant's index → inject into prompt.
- **The retrieval scope is the access boundary.** The same scoped STS session that
  authorizes which models a user may call also determines which vector index and S3
  prefix it may read. A student enrolled in CHEM-101 (roster from LTI NRPS) gets a
  session tag binding them to that course's index and nothing else.

FERPA is therefore not a feature layer — it falls out of the IAM/ABAC scoping that
already governs the model call.

---

## 5. Identity & enterprise integration

- **Federation:** the campus IdP — Shibboleth/InCommon (SAML), Entra or Okta (OIDC) — is
  already wired campus-wide; the Cognito Identity Pool **hooks into the existing
  federation**, it does not introduce a new identity system. No Cognito **User Pool** of
  local accounts (avoid the MAU cost).
- **Attributes:** map eduPerson affiliation (`student`/`faculty`/`staff`/`member`) and,
  where available, enrolled-course identifiers into session tags.
- **Affiliation → entitlement** (drives model tier *and* budget):
  - `student` → gpt-oss / smaller models, tight per-term cap
  - `faculty`/`staff` → mid + frontier with a department budget
  - `researcher` (grant-tagged) → frontier models, grant-funded budget
- This mapping lives in **AgentCore Policy** (Cedar) for the agent path and in the
  IAM/role scope for the chat path — not in code branches.

---

## 6. LMS integration — one connector, not N

The entire LMS story is **LTI 1.3** (1EdTech). A single tool registration launches
inside Canvas, Anthology/Blackboard, Moodle, and Brightspace alike.

- **OIDC login init**, **launch**, **JWKS**, and **deep-linking return** endpoints — all
  request/response, all serverless (Lambda + API Gateway HTTP API). State (nonce,
  platform registration) in DynamoDB on-demand. No clock.
- **NRPS** (Names and Role Provisioning Services) → rosters/roles → the enrolled-course
  session tags used for retrieval scope.
- **Deep Linking** → embed `agg` activities in a course.
- **SIS (Banner/PeopleSoft) is deliberately OUT OF SCOPE.** An AI assistant needs
  roster + role context, which LTI already carries. Deep SIS integration is the thorny
  thing you don't need.

---

## 7. Multi-tenancy & chargeback

The university org chart **is** the tenancy model.

- Colleges/departments/courses = tenants = cost centers.
- Tag every Bedrock invocation and every S3 Vectors index with the tenant
  (application inference profiles for Bedrock cost attribution; ABAC tags on vectors).
- Spend reporting: aggregate Bedrock invocation logs + Cost Allocation Tags → per-tenant
  chargeback.

### 7.1 Spend controls — soft cap (no mid-stream kill)

You never kill a call in flight; you decline to **start** the next one.

- Every request carries a per-tier `max_tokens`, giving each call a known cost ceiling.
- The **soft cap is a budget read inside the identity broker** (the component from §3.1).
  At each credential issue/refresh the broker checks accumulated spend vs budget; over
  budget → it vends no model creds (or read-only). Because creds are short-lived
  (TTL 5–15 min), an over-budget user loses model access at the next refresh.
- Max overrun = (calls makeable in one TTL window) × per-call ceiling, bounded further by
  a per-user rpm limit. Tighten the TTL and it's negligible. No in-flight cutoff needed.
- **Hard pre-call enforcement** (Tier 1) is available if an institution requires exact
  caps: route inference through the meter Lambda, which assumes the *user's own* scoped
  role and rejects a call when `input + max_tokens` estimate would exceed budget.

### 7.2 Real-time metering — computed dollars, itemized

The meter computes **actual dollars** from authoritative usage and live rates, itemized
per call — lifted from the `CostMeter` pattern in `aws-agencore-demo` (`cost.py` +
`pricing.py`). It is **not** an opaque token tally.

- **Cost engine (`cost/`):** a pure, side-effect-free `CostMeter` (testable without AWS).
  Each call produces a `CostRow`; USD = `inputTokens`/`outputTokens` (returned in the
  Bedrock `converse()` response itself) × per-million rates. The same engine costs LLM
  calls, embeddings/ingestion, KB retrieval (per-1k queries), and Code Interpreter
  (wall-clock seconds). Output is an itemized **receipt** (rows + total) that doubles as
  per-user/per-tenant chargeback — strictly more than LiteLLM's aggregate counter.
- **Live estimate (UX):** the browser ports the same engine and shows a running receipt
  from its own response `usage`. Display only — it carries **no authority**.
- **Authoritative spend (enforcement):** a Lambda computes the canonical number from
  **Bedrock invocation logging** (the logged token counts) × the same rates and writes it
  to the `spend` table. The soft cap (§7.1) reads *this*, never the client estimate — so
  an untrusted client cannot inflate its own budget.
- **Standing-endpoint flag:** if a call targets a Rung-2 bring-your-own SageMaker endpoint
  (§2.5), the receipt marks it as a **standing** cost ("$X/hr whether idle or not") — the one
  place the design permits a clock is made visible at point of use, not buried in the bill.
- **Rates (`pricing.py`):** fetched live from the AWS Price List API (boto3 `pricing`,
  us-east-1 only), cached at startup, with config + hard-default fallbacks.
- **Data model:** `spend` table, PK `{tenant}#{user}#{period}` (+ `{tenant}#{period}`
  rollup); period in the key for rollover.

**Price List quirks to respect (verified in `pricing.py`, 2026-05):** S3 Vectors is not
yet in the Price List (use config fallbacks); Claude 4.x prices live under
`AmazonBedrockFoundationModels`, *not* `AmazonBedrock`; the Price List API is us-east-1
only; usagetype prefixes are regional (`USW2`, not `us-west-2`).

**On LiteLLM:** its budget engine is a server (the box we avoid) and only tallies tokens.
We use the `CostMeter` instead — thinner, itemized, authoritative. LiteLLM returns only
as the optional Tier 2 batteries.

---

## 8. Governance — FERPA / HIPAA control mapping

| Control | Requirement | How `agg` satisfies it |
|---|---|---|
| Data residency | Education records stay in institutional control | Everything in the institution's own AWS account; Bedrock inference stays in-account/in-region |
| No third-party retention | Records not used to train external models | Bedrock-hosted models (incl. OpenAI GPT-5.x, Anthropic, Llama, Mistral, gpt-oss) — no provider-side retention; **on-demand only** |
| Per-record isolation | Course/dept data not commingled | Per-tenant S3 prefix + per-index CMK + ABAC retrieval scope |
| Least privilege | Users reach only their data | Scoped STS session derived from affiliation + enrollment |
| Auditability | Prove who accessed what | CloudTrail + Bedrock invocation logging + (Tier 1) gateway access/spend logs |
| Policy enforcement | Machine-checkable governance | **AgentCore Policy** (Cedar) for agent actions/tools + IAM/ABAC for chat — a CISO can read and enforce it |
| Agent containment | Tool / code / web access bounded | **AgentCore Runtime** per-session microVM isolation; **Gateway + Policy** gate every tool call (user-scoped); **Code Interpreter** sandboxed |
| Responsible-AI safeguards | Bias/misuse/leakage controls | **Bedrock Guardrails** (Tier 0 per-role, or Tier 1 centralized) |
| HIPAA (academic med center) | BAA-covered PHI workloads | Same stack under BAA; NET+ AWS already offers BAA terms |

This is a *stronger* FERPA posture than any SaaS AI tool: "data never leaves your
control, and we can prove it" is true by construction, not by contract.

**Agent path isolation.** When a user invokes an agent, the same boundary holds: the
user's identity flows into the AgentCore session (inbound auth via AgentCore Identity,
fed by the campus IdP through Cognito), **AgentCore Policy** (Cedar) authorizes which
tools/actions the agent may take *for this user*, and the tools behind **AgentCore
Gateway** enforce the user's tenant/course scope (they assume the user's scoped role or
apply the same ABAC). The agent runs in a per-session microVM that is destroyed at
session end. So "one boundary governs model and data" extends cleanly to agents —
expressed through AgentCore Policy + scoped tools rather than a standalone Cedar plane.

---

## 9. Cost model

**Idle:** ~$0 (storage pennies only). The bill *is* the consumption.
**Per-use, only when used:**
- Bedrock: per-token (on-demand; **never** provisioned throughput — that's a clock).
- S3 Vectors: per vector stored + per query (no OCU floor; ~90% cheaper than
  OpenSearch Serverless, which carries a ~$175–350/mo idle floor and the silent
  "deleting a KB doesn't delete the collection" footgun — avoided entirely here).
- Lambda / HTTP API / DynamoDB on-demand: per-request.
- CloudFront / S3: per-request / per-GB.

**Networking:** default to **no VPC** for the lean path — Bedrock and S3 are reached
via AWS APIs over the institution's existing, already-amortized egress, so there is no
NAT gateway (a $0.045/hr clock × AZs). If a security policy forces VPC isolation,
prefer the **S3 gateway endpoint (free)**; note that **interface VPC endpoints bill
hourly** — that's a clock, so require a named justification before adding one.

---

## 10. Match & exceed nebulaONE

| nebulaONE claim | `agg` match | `agg` exceeds by |
|---|---|---|
| "All the top models" | Bedrock hosts OpenAI GPT-5.x, Anthropic, Llama, Mistral, DeepSeek, gpt-oss | Models run **in-boundary** (IAM/VPC/region), not as external API calls |
| "In your tenant" | Institution's own AWS account | True data sovereignty incl. OpenAI models in-account; Azure can't claim the in-boundary OpenAI story post-Bedrock |
| "Pay only for consumption" | Per-token, per-vector, per-request | **Zero standing cost** — structural, not a billing toggle; no per-seat license at all |
| "Secure / Responsible AI" | Guardrails + audit | **AgentCore Policy** (Cedar) + per-session microVM isolation — auditable, AWS-managed, not a marketing line |
| "Deploy AI agents in hours" | AgentCore Runtime + Gateway tools | Managed serverless agents, microVM-isolated, scale-to-zero, Cedar-governed tool access, user-scoped |
| "Low-code / visual agents" | **Bedrock Flows** (serverless visual canvas) | No server to run — flows execute serverless (~$0.035/1k node transitions), in-account, reuse the same Guardrails/KB/models |
| "Campus connectors" | LTI 1.3 (one registration, all major LMS) | No bespoke connector sprawl; open and standard |
| "Managed platform" | Static + serverless, self-owned | **Open source**, cloud-portable by construction, no vendor capture |
| Single-cloud (Azure-only) | AWS-native | Architecture is portable; lock-in is at neither tenant nor capability layer |

### 10.1 Visual / low-code authoring (the nebulaONE "low-code agents" answer)

Visual builders split into two shapes, and only one fits the posture.

- **Default — Amazon Bedrock Flows.** AWS-native, **serverless** visual builder: link models,
  prompts, agents, knowledge bases, Guardrails, and AWS services on a canvas; run with no
  infrastructure to deploy, billed purely per **node transition** (~$0.035/1k, no idle floor).
  It stays **in the institution's account** and reuses `agg`'s models / KB / Guardrails. This
  matches nebulaONE's low-code-agent pitch with **no server and no clock**.
- **Optional — Langflow as a *design-time* tool.** For teams that want a richer canvas, use
  Langflow to author **visually**, then **export to LangChain / LangGraph Python and deploy
  that code to AgentCore Runtime** (Langflow can also emit MCP servers, which slot straight
  into AgentCore Gateway). The discipline: Langflow is an **authoring** tool here, not a
  hosted runtime — the runtime stays serverless (AgentCore), so no standing box is introduced.
  It also gives a clean "graduate from canvas to code" path when a flow outgrows the GUI.
- **Off the critical path — Flowise / Dify / n8n.** These are **persistent servers** (a 24/7
  container = a clock *and* an ops/patch burden), so they are not `agg` components. An
  institution may run one itself and point it at `agg`'s Bedrock access, but that clock is
  theirs to own, by name.

Same rule as the whole design: a visual layer is welcome only if it doesn't reintroduce a
server or a clock. Bedrock Flows doesn't; Langflow-as-authoring doesn't; Flowise/Dify-as-
runtime do.

---

## 11. Repo layout

```
aws-genai-gateway/
├── infra/                 # AWS CDK v2 (aws-cdk-lib, Python 3.12+). One app, small focused stacks.
│   ├── app.py
│   └── stacks/
│       ├── identity.py    # Cognito Identity Pool + federation + roles  ← load-bearing
│       ├── data.py        # S3 buckets, S3 Vectors indexes, CMKs
│       ├── audit.py       # invocation logging, CloudTrail, log bucket
│       ├── lti.py         # HTTP API + Lambdas + DynamoDB (LTI 1.3)
│       ├── agent.py       # AgentCore Runtime + Gateway + Policy + Memory wiring (agent path)
│       ├── meter.py       # spend table + invocation-log cost Lambda (CostMeter) (Tier 0)
│       ├── chokepoint.py  # OPTIONAL (Tier 1): Lambda Function URL + budget table
│       └── web.py         # S3 + CloudFront for the SPA
├── web/                   # Static SPA (minimal, no server). Vite + React or plain TS.
│   ├── src/transport/
│   │   ├── bedrock.ts     # Tier 0 adapter: AWS SDK v3 BedrockRuntimeClient + Cognito creds
│   │   ├── openai.ts      # Tier 1/2 adapter: fetch against Function URL / LiteLLM
│   │   └── agentcore.ts   # agent-path adapter: invoke an AgentCore Runtime agent
│   ├── src/auth/          # IdP redirect, Cognito identity exchange, creds refresh
│   ├── src/rag/           # client-side embed-query + S3 Vectors query (scoped creds)
│   └── src/chat/          # streaming chat UI, history (local or AgentCore Memory)
├── lti/                   # LTI 1.3 tool provider handlers (login/launch/jwks/deeplink)
├── agent/                 # agent defs + Gateway tool specs (MCP/Lambda) + Cedar policies
├── policy/                # Cedar policies (AgentCore Policy) + IAM role/trust templates + tag scheme
├── ingest/               # embed-on-upload Lambda (alt. to Bedrock KB)
├── cost/                 # CostMeter + pricing.py (lifted from aws-agencore-demo)
├── meter/                # invocation-log → `spend` table (authoritative); live-estimate path
├── cli/                   # `agg` admin CLI (Go) — deploy helpers, tenant/budget mgmt, ingest
└── docs/
```

**Toolchain (pinned — don't guess):**
- **IaC:** AWS **CDK v2** (`aws-cdk-lib`, recent pin ≥ 2.215; there is no v3), **Python** binding —
  *not* TypeScript. Chosen so IaC, Lambdas, and the cost engine share one language, and because the
  audience reads and forks Python, not TS. New Bedrock / AgentCore / S3-Vectors resources: use L1
  `Cfn*` constructs where no L2 exists yet (L1s are generated from the CloudFormation spec and are
  current across all language bindings, so Python is not behind for the newest services).
- **Python:** target **3.12 or 3.13** for the CDK app *and* every Lambda runtime. **Never 3.9** — it
  hit EOL in Oct 2025 and Lambda blocks new 3.9 functions as of 2026-02-03. Keeping a recent CDK pin
  also moves CDK's own bundled custom-resource handler Lambdas onto a supported runtime.
- **Node.js is required even though IaC is Python:** the `aws-cdk` CLI is an npm package and the
  Python bindings call into a Node jsii runtime under the hood, so dev boxes and CI need Node present.
  State this in the README so nobody is surprised.
- **Package management:** `uv` for Python; Go toolchain for `cli/`.
- On-demand everything. No VPC unless forced.

---

## 12. Implementation order for Claude Code

Build the hard, foundational thing first; everything else depends on its tag scheme.

**Phase 0 — Toolchain bootstrap.**
- Pin **`aws-cdk-lib` v2** (recent ≥ 2.215) + `constructs`; `uv`-managed Python **3.12/3.13**;
  ensure **Node.js** is installed for the `aws-cdk` CLI + jsii runtime. `cdk bootstrap` the target
  account/region (modern `DefaultStackSynthesizer`). Default all Lambda runtimes to 3.12/3.13.

**Phase 1 — Identity broker + ABAC (the crux).**
- CDK `identity.py`: Cognito Identity Pool, SAML/OIDC federation, authenticated role.
- Define the **session-tag scheme** (§13.1) and the authenticated role's permissions
  boundary that reads those tags.
- Prove end-to-end: federate a test IdP user → receive STS creds scoped by tag →
  `bedrock:InvokeModel`/`Converse` succeeds for entitled models, denies others.

**Phase 2 — Static SPA, Tier 0 transport.**
- `web/`: auth flow → Cognito creds → `transport/bedrock.ts` streaming Converse.
- No history persistence yet (in-memory). Confirm browser-direct streaming works.

**Phase 3 — Data plane (RAG).**
- CDK `data.py`: per-tenant S3 prefix, S3 Vectors index, CMK.
- `ingest/` embed-on-upload Lambda (or wire Bedrock KB).
- `web/src/rag/`: query-embed → scoped S3 Vectors query → context injection.
- Verify a CHEM-101-scoped session can read only the CHEM-101 index.

**Phase 4 — LTI 1.3 tool provider.**
- `lti/` + CDK `lti.py`: login/launch/jwks/deeplink, registration store in DynamoDB.
- NRPS → enrolled-course tags feeding the Phase 1 scheme.
- Launch inside a Canvas test instance.

**Phase 5 — Governance, audit + real-time metering.**
- `policy/`: Cedar policies (for **AgentCore Policy**) encoding affiliation→entitlement;
  attach Guardrails.
- `audit.py`: invocation logging + CloudTrail + cost-allocation tags + chargeback query.
- `cost/` + `meter.py`: lift `CostMeter`/`pricing.py` from `aws-agencore-demo`; compute
  **authoritative** spend server-side from Bedrock invocation logging × Price List rates
  into the `spend` table; wire the soft-cap read into the Phase 1 broker. Port `CostMeter`
  to TS for the SPA's live (non-authoritative) cost display and itemized receipt.

**Phase 6 — Optional Tier 1 choke point.**
- `chokepoint.py`: Lambda Function URL (response streaming) **assuming the user's own
  scoped role**, with an exact pre-call budget check (authoritative metering).
- Flip SPA transport to `openai.ts`. Only build if an institution requires exact caps,
  centralized guardrails/PII inspection, or non-Bedrock routing.

**Phase 7 — CLI + packaging.**
- `cli/`: `agg deploy`, `agg tenant add`, `agg budget set`, `agg ingest`.
- One-command bootstrap; optional AWS Marketplace (free) listing for procurement parity.

**Phase 8 — Agent path (AgentCore).**
- `agent/` + `agent.py`: define agents on **AgentCore Runtime**; expose tenant-scoped
  tools via **Gateway** (Lambda/MCP); author **AgentCore Policy** (Cedar) for tool/action
  authorization; wire **Identity** (inbound = the user via Cognito; outbound = scoped tool
  creds), **Memory** for agent state, **Code Interpreter** for execution.
- `web/src/transport/agentcore.ts`: invoke the runtime agent; render tool/Cedar events.
- Extend `CostMeter` to include AgentCore active-vCPU/GB-seconds, Gateway calls, Memory
  ops in the receipt. Start from the `aws-agencore-demo` agent loop — it already does
  Gateway + Cedar + Code Interpreter single-tenant.
- Set **short AgentCore session idle timeouts** and keep AgentCore **out of a VPC** (see
  Non-goals) so the agent path stays clock-free.

---

## 13. Load-bearing spec details (skeletons for Claude Code)

### 13.1 Session-tag scheme
Tags set at credential-vend time from IdP claims:
```
agg:affiliation   = student | faculty | staff | researcher
agg:tenant        = <college/dept id>            # cost center + isolation key
agg:courses       = <comma list of course ids>   # from LTI NRPS, drives retrieval scope
agg:tier          = oss | mid | frontier         # derived from affiliation/grant
```

### 13.2 Authenticated role — Bedrock scope (sketch)
Allow `bedrock:InvokeModel*` / `bedrock:Converse*` **only** for model ARNs whose tier
tag matches `agg:tier`. Deny by default. (Express the tier→model-ARN map as a managed
policy or via Cedar pre-authorization, not inline branches.)

### 13.3 Authenticated role — data scope (sketch)
Allow `s3:GetObject` on `arn:aws:s3:::agg-docs/${aws:PrincipalTag/agg:tenant}/*` and the
S3 Vectors query action only on the index tagged with the same `agg:tenant` /
`agg:courses`. The principal tag is the isolation primitive.

### 13.4 Cedar policy sketch (chat scope + AgentCore Policy)
```
permit(principal, action == Action::"InvokeModel", resource)
when {
  resource.tier == principal.tier &&
  resource.tenant == principal.tenant
};
permit(principal, action == Action::"Retrieve", resource)
when { resource.index_tenant in principal.courses };
// agent path: AgentCore Policy authorizes tool/action use per user
permit(principal, action == Action::"CallTool", resource)
when { resource.tool in principal.allowed_tools &&
       resource.tenant == principal.tenant };
```
For the chat path, Cedar is the human-auditable layer; compile/mirror its intent into the
IAM scope above. For the agent path, the same policies are loaded into **AgentCore
Policy**, which enforces them natively on every tool/action call.

### 13.5 LTI 1.3 endpoints
`/lti/login` (OIDC init) · `/lti/launch` (id_token validation → mint agg session) ·
`/.well-known/jwks.json` · `/lti/deeplink` (return). Registration + nonce in DynamoDB.

### 13.6 Cost meter + soft cap
Lift `cost.py`/`pricing.py` from `aws-agencore-demo` into a shared `cost/` lib.
`CostMeter` computes USD per call from the `usage` in each Bedrock response × live Price
List rates, emitting an itemized receipt (LLM, embeddings, retrieval, compute).
**Authoritative spend** for the soft cap is recomputed server-side from Bedrock
invocation logging (logged token counts × same rates) into the `spend` table, PK
`{tenant}#{user}#{period}` (+ `{tenant}#{period}` rollup) — so the enforcement number is
log-derived, not client-reported. The browser runs the same engine for a live UX estimate
only. Soft cap = broker reads authoritative spend at creds refresh.

### 13.7 Agent path (AgentCore)
- **Runtime & the framework contract:** AgentCore Runtime is **framework-agnostic and
  model-agnostic** — it takes any local agent code (Strands, LangGraph, CrewAI, LlamaIndex,
  Autogen, OpenAI Agents SDK, Google ADK, or no framework) on any model, communicating over
  **MCP / A2A**. So `agg` does **not** build framework support — it defines a thin *contract*: a
  containerized agent that honors the AgentCore invocation protocol, and ships a **Strands
  reference agent** as the default. Institutions swap in their framework of choice without
  touching the surrounding boundary. The SPA invokes the agent via `agentcore.ts`. Set a short
  session idle timeout.
- **Why this is free differentiation:** the `agg` value — identity-scoped creds, CostMeter
  metering, Cedar policy, Guardrails — sits at the AgentCore *boundary*, not inside the agent,
  so it is identical across every framework. You support all of them by owning none.
- **Identity:** inbound auth validates the invoking user (Cognito → campus IdP);
  outbound auth vends scoped creds for tools. The user's `agg:tenant`/`agg:courses` flow
  into the session so tools and retrieval stay user-scoped.
- **Gateway:** register tenant-scoped tools (Lambda/MCP); the retrieval tool is the same
  scoped S3 Vectors query as the chat path. **Policy** (Cedar, §13.4) gates every
  `CallTool`.
- **Code Interpreter / Memory:** sandboxed execution; short/long-term agent state.
- **Cost:** extend `CostMeter` with AgentCore active-vCPU/GB-seconds, Gateway invocations,
  Memory ops — itemized on the same receipt.
- **Reuse:** `aws-agencore-demo` already implements this single-tenant (Gateway + Cedar +
  Code Interpreter + the cost meter). agg wraps it with the claims→scope broker and tenancy.

---

## 14. Non-goals / anti-over-engineering guardrails (for the implementer)

- **No VPC** by default. No NAT gateway, ever, on the lean path.
- **No OpenSearch** anything (the idle-floor + KB-deletion footgun). S3 Vectors only.
- **No provisioned Bedrock throughput.** On-demand only.
- **No always-on container** (no Fargate/ALB/RDS/ElastiCache) unless Tier 2 is
  explicitly chosen with a named justification.
- **No AgentCore in a VPC** unless forced — Gateway VPC egress + PrivateLink are clocks.
  And set **short AgentCore session idle timeouts** so abandoned sessions don't accrue
  held-memory billing against the default timeout.
- **No AgentCore on the plain-chat path.** Agents are for agentic work; a single Converse
  stays browser-direct. Don't pay Runtime for a chat turn.
- **No Cognito User Pool** of local accounts unless explicitly required.
- **No SIS integration.** LTI carries the context you need.
- **No self-hosted visual-builder server** (Flowise/Dify/n8n) on the critical path —
  Bedrock Flows is serverless; Langflow is authoring-only (export → AgentCore). (§10.1)
- **No standing model endpoint by default.** Rung 0/1 (managed FM, Custom Model Import) are
  serverless; a Rung-2 SageMaker endpoint is a clock and ships only on a named requirement,
  flagged in the receipt. (§2.5)
- **No "platform."** If a proposed component bills by the hour, it must earn it against
  a named institutional requirement. Default answer is the edge.

---

*End of design. The two things worth getting right are the identity→scoped-STS broker
and the ABAC tag scheme (§3.1, §13). Everything else is assembly.*
