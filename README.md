# agate

An open-source, **identity-native, serverless GenAI access layer for higher education** with
**no fixed cost floor** in the default path. A person federates their campus identity and
receives short-lived credentials scoped to exactly the models and documents they're entitled
to. From there agate uses **direct AWS access where the service and authorization boundaries
permit it, and narrow serverless mediation where they don't** — with all four paths fenced by
the *same* ABAC boundary:

| Path | How it runs |
|------|-------------|
| **CLI / native** model call | scoped credentials → Amazon Bedrock directly |
| **Browser** model call ("Ask") | browser → serverless **chokepoint** → Bedrock *(Bedrock's runtime has no browser CORS)* |
| **Scoped retrieval** | browser → **retrieval proxy** → S3 Vectors *(IAM can't express the sub-tenant filter alone)* |
| **Agent** execution | user authority → **AgentCore**-mediated run |

"No fixed cost floor" means the default path adds **no bill-by-the-wall-clock-hour compute or
network** (see NO CLOCKS below); usage-based storage, logs, retained artifacts, requests, and
opt-in services (e.g. memory) still incur cost.

- **Docs — start here (task-oriented index):** [`docs/index.md`](docs/index.md)
- **What's real vs experimental vs vision:** [`docs/product/maturity-matrix.md`](docs/product/maturity-matrix.md)
- **How requests actually flow:** [`docs/architecture/request-paths.md`](docs/architecture/request-paths.md)
- **Architecture (source of truth):** [`docs/agate-design.md`](docs/agate-design.md)
- **Agent platform vision (where it goes):** [`docs/agate-agents-vision.md`](docs/agate-agents-vision.md) — agate as the governance layer over the open agent stack; *agenkit builds the agent, agate governs it* ([agenkit.dev](https://agenkit.dev))
- **Security rationale (CISO memo):** [`docs/agate-security-ciso.md`](docs/agate-security-ciso.md)
- **Coding contract:** [`CLAUDE.md`](CLAUDE.md)

> Project, repo, package, and CLI binary are all **`agate`** (named for agate, a banded form
> of bedrock). Session-tag namespace `agate:`; docs bucket prefix `agate-docs`. The name is
> still provisional ("for now").

## A quick tour

<!-- Screenshots pending (#220). Drop PNGs in docs/images/ and uncomment:
![Ask — grounded answer with citations and a cost receipt](docs/images/ask.png)
![Notebook — a code cell rendering a matplotlib plot inline](docs/images/notebook.png)
![Context panel — send window / clear / compress, with the running cost meter](docs/images/context.png)
-->

The SPA has **Ask** (grounded chat with citations + a per-answer cost receipt), a **Notebook**
view (prompt cells + client-side Python code cells that render plots inline), a **Context** panel
(clear / sliding-window / compress), and — when their stacks are deployed — Documents, Memory,
Rooms, drafting/authoring, and an admin console. Screenshots are tracked in
[#220](https://github.com/scttfrdmn/agate/issues/220); see [`docs/images/`](docs/images/).

## The governing principle: NO CLOCKS

Nothing in the default (Tier 0) design bills by the wall-clock hour while idle — no NAT
gateway, no OpenSearch, no provisioned Bedrock throughput, no always-on container, no
interface VPC endpoints. Every component bills per-request or per-byte-stored. Idle cost ≈
the rent on the documents sitting in S3.

## The two load-bearing parts

Everything else is assembly. The engineering care lives in:

1. **The claims → scoped-STS broker** — translate the campus IdP's existing claims
   (eduPerson affiliation, enrolled courses) into AWS session tags, and vend a temporary
   credential = the authenticated role *narrowed by those tags*.
2. **The ABAC tag scheme** — one `agate:` tag scheme that simultaneously governs which Bedrock
   models a session may invoke and which S3 prefixes / S3 Vectors indexes it may read.

## Repository layout

```
infra/    AWS CDK v2 (aws-cdk-lib, Python 3.13). One app, small focused stacks.
          stacks/identity.py is the load-bearing Phase 1 stack.
          lambda/broker/      per-request credential-vending broker (scales to zero).
web/      Static SPA (Vite + TypeScript). Three swappable transport adapters.
cli/      `agate` admin CLI (Go).
policy/   IAM/Cedar tag scheme + role/trust templates.
cost/     CostMeter + pricing (pure, testable without AWS).      [later phase]
meter/    invocation-log -> spend table (authoritative).         [later phase]
lti/      LTI 1.3 tool provider handlers.                        [later phase]
agent/    AgentCore agent defs + Gateway tool specs.             [later phase]
ingest/   embed-on-upload Lambda.                                [later phase]
docs/     design + security docs.
tests/    pure-logic unit tests (no AWS).
```

## Toolchain (pinned — don't guess)

| Tool | Version | Why |
|---|---|---|
| AWS CDK v2 (`aws-cdk-lib`) | `>=2.215,<3` | IaC. There is no v3. **Python** binding, not TS. |
| Python | **3.12 / 3.13** | CDK app *and* every Lambda runtime. Never 3.9 (EOL). |
| `uv` | latest | Python env + dependency management. |
| Node.js | 20+ | Required even though IaC is Python — the `aws-cdk` CLI is npm and the Python bindings call a Node jsii runtime. |
| Go | 1.22+ | The `agate` CLI in `cli/`. |

## Quickstart (dev)

```bash
# Everything runs from the REPO ROOT — pyproject.toml, uv.lock, cdk.json and the
# aws-cdk CLI's package.json all live at the root (there is no infra/ project).
uv sync                       # install CDK + deps into .venv
uv run pytest                 # pure-logic unit tests (no AWS)
uv run ruff check && uv run ruff format

# CDK synth (needs Node for the aws-cdk CLI + jsii). Install the repo-pinned CLI and use it
# via `npm run` (or ./node_modules/.bin/cdk) — NOT a global `npx cdk`, which can be stale and
# fail with a "cloud assembly schema version mismatch" against the newer aws-cdk-lib.
npm install                    # installs the pinned aws-cdk CLI (schema-compatible with aws-cdk-lib)
npm run synth                  # -> cdk synth   (likewise: npm run diff / npm run deploy)

# Go CLI
(cd cli && go build ./... && go test ./...)
```

### `cdk bootstrap` notes

CDK needs a one-time bootstrap per **account + region** before the first deploy. Use the
modern `DefaultStackSynthesizer` (the default in recent CDK):

```bash
# Read-only checks (safe): npx cdk synth, npx cdk diff
# Bootstrap is a privileged, account-mutating action — run it yourself, with intent:
npx cdk bootstrap aws://<ACCOUNT_ID>/<REGION>
```

Bootstrap provisions a CDK toolkit stack (an S3 staging bucket, ECR repo, and deploy roles).
None of it carries a wall-clock clock — it is storage + IAM only, consistent with NO CLOCKS.
Pin the region to where Bedrock + S3 Vectors are available for your institution.

## Deploying a demo

**Evaluators: one command.** With AWS credentials set (`AWS_PROFILE=… ` or env creds) and `uv`,
`node`, `aws`, `jq` installed:

```bash
git clone https://github.com/scttfrdmn/agate && cd agate
./scripts/deploy-demo.sh          # validates prereqs, shows a plan, asks before deploying
```

It deploys only the smallest coherent path (`agate-identity`, `agate-data`, `agate-audit`,
`agate-chokepoint`, `agate-web`, `agate-demo-idp`), derives the web config from stack outputs,
runs a smoke test, and prints the URL, how to create a demo user, cost notes, and the exact
teardown command. The manual sequence below remains the reference for a real/campus deploy.

---

The stacks are independent; deploy only what a given demo needs. The Tier 0 path
(`agate-identity`) is $0-idle and the safest first deploy; the data/agent/web stacks add
storage + a container. The CDK app currently defines 16 stacks — the load-bearing core
(`agate-identity`, `agate-data`, `agate-audit`), the browser demo (`agate-web`,
`agate-chokepoint`, `agate-demo-idp`), and optional product/experimental stacks
(`agate-agent`, `agate-governance`, `agate-corpus`, `agate-memory`, `agate-rooms`,
`agate-drafting`, `agate-authoring`, `agate-deploy`, `agate-admin`, `agate-lti`). See the
[maturity matrix](docs/product/maturity-matrix.md) for what each is and how settled it is
(Available / Experimental / Seam / Vision).

**0. Refresh per-model pricing (optional, recommended).** Bake authoritative Bedrock
list rates into the cost engine before deploying the metering stacks. Read-only against
AWS (`pricing:GetProducts`, us-east-1); without it the engine uses live-verified hard
defaults, so this is a refresh, not a prerequisite.
```bash
uv run python -m cost.pricelist --out cost/model_rates.json   # generated artifact, gitignored
```
The meter/chokepoint Lambdas load `cost/model_rates.json` automatically (it ships inside
the bundled `cost` package) — no env var, no runtime Price List call (NO CLOCKS).

**1a. Demo IdP FIRST (only if you have no campus IdP to point at).** Deploy it before the broker
so its OIDC outputs are available — and pass the **deployed SPA URL** as `site_url` so the Hosted
UI will redirect back to it (omit it and login errors with "An error was encountered with the
requested page" because the CloudFront origin isn't a registered callback):
```bash
npx cdk deploy agate-demo-idp -c site_url=https://<cloudfront-domain>   # throwaway Cognito pool
```
(First time you won't know the CloudFront domain yet — deploy `agate-web` once, then RE-deploy
`agate-demo-idp` with its `SiteUrl`. The re-deploy modifies the app client in place; the
UserPoolId/audience don't change.) The stack outputs `OidcIssuer`, `OidcJwksUrl`, `OidcAudience`.

**1. Identity (the crux — Tier 0).** The broker verifies the IdP token, so it needs the OIDC
config as deploy **context** (`-c`) — without it the broker fails closed ("verifier
misconfigured"). Pass the demo-idp outputs (or your campus IdP's values):
```bash
npx cdk deploy agate-identity \
  -c oidc_issuer=<OidcIssuer> -c oidc_jwks_url=<OidcJwksUrl> -c oidc_audience=<OidcAudience>
```
The same three values also wire the agent's gateway as `cognito_discovery_url` /
`cognito_audience`, and the new endpoint stacks (drafting/authoring/deploy/rooms/memory) take
`-c cognito_discovery_url=<…/.well-known/openid-configuration> -c cognito_audience=<OidcAudience>`
(they derive issuer + JWKS from the discovery URL). Create a demo user and set their
`custom:affiliation` / `custom:tenant` / `custom:courses` attributes — a pre-token Lambda maps
those onto the `agate` claims, so the demo token scopes exactly like a campus token. Production
skips this stack entirely.

**2. Data + a demo corpus (Ask/RAG).**
```bash
npx cdk deploy agate-data -c tenants=demo
agate tenant add demo                    # the CLI tracks tenants/budgets
agate ingest --tenant demo --bucket agate-docs-<acct>-<region> ./sample.pdf --confirm
```

**3. Agent path (Panel/Analyze).** Build + push the reference container, then deploy:
```bash
docker build -t agate-agent ./agent && \
  docker tag agate-agent <ecr-repo>:latest && docker push <ecr-repo>:latest
npx cdk deploy agate-agent -c agent_container_uri=<ecr-repo>:latest \
  -c cognito_discovery_url=<oidc-discovery-url> -c cognito_audience=<app-id>
npx cdk deploy agate-governance          # Guardrails + Cedar policies (optional but recommended)
```

**4. Web (the SPA).** Build with the deployed endpoints, then host:
```bash
cd web && VITE_BROKER_URL=<broker-url> VITE_AWS_REGION=<region> \
  VITE_COGNITO_DOMAIN=<hosted-ui-domain> \
  VITE_COGNITO_CLIENT_ID=<oidc-audience> \
  VITE_RETRIEVAL_URL=<retrieval-url> \
  VITE_AGENT_RUNTIME_ARN=<runtime-arn> \
  VITE_DRAFTING_URL=<drafting-url> \
  VITE_DEPLOY_URL=<deploy-url> \
  VITE_AUTHORING_URL=<authoring-url> \
  VITE_ROOMS_URL=<rooms-url> \
  VITE_CORPUS_URL=<corpus-url> \
  VITE_MEMORY_URL=<memory-url> \
  VITE_CHOKEPOINT_URL=<chokepoint-url> npm run build && cd ..
npx cdk deploy agate-web                 # publishes web/dist to S3 + CloudFront
```
The `agate-web` output `SiteUrl` is the demo URL. `VITE_COGNITO_DOMAIN` (the `agate-demo-idp`
output `HostedUiDomain`) and `VITE_COGNITO_CLIENT_ID` (its `OidcAudience` — the app client id)
wire the **Log in / Log out** button to the demo Hosted UI; omit them and the SPA hides the auth
control and expects a manually pasted `#idp_token=<jwt>` in the URL hash instead. `VITE_RETRIEVAL_URL` is the
`agate-identity` output `RetrievalUrl` — the broker-proxied vector retriever that
enforces sub-tenant scope (#84); omit it to disable RAG grounding. `VITE_DRAFTING_URL`
is the `agate-drafting` output `DraftingUrl` (#118b) — the natural-language "Draft an
agent" screen; omit it to hide that screen. `VITE_DEPLOY_URL` is the `agate-deploy`
output `DeployUrl` (#118) — the confirm-and-create action; omit it and drafts render
but the confirm button stays inert. `VITE_AUTHORING_URL` is the `agate-authoring`
output `AuthoringUrl` (#117) — the visual "Build an agent" screen (bounded menu +
form); omit it to hide that screen. `VITE_ROOMS_URL` is the `agate-rooms` output
`RoomsUrl` (#116) — the collaborative "Rooms" screen (polling transport); omit it to
hide that screen. `VITE_CORPUS_URL` is the `agate-corpus` output `CorpusUrl` (#191) — the
"Documents" screen to upload + browse your in-scope corpus; omit it to hide that screen.
`VITE_MEMORY_URL` is the `agate-memory` output `MemoryUrl` (#194) — when set (and the
**opt-in, billable** `agate-memory` stack is deployed), Ask recalls cross-session memory
before each turn and records the turn after; omit it and Ask keeps browser-only history.
`VITE_CHOKEPOINT_URL` is the `agate-chokepoint` output `ChokepointUrl` —
when set, **Tier-0 "Ask" routes through the choke point** (gated + metered, server-enforced)
instead of browser-direct Bedrock; **required for Ask to work in the browser** (Bedrock's
runtime endpoint has no CORS, so a web-origin call to it is blocked). Omit it and Ask stays
browser-direct (works from a CLI/native caller only).

**5. Tier 1 choke point** (`agate-chokepoint`) — deploy it to gate/meter Ask (above) or for
exact pre-call caps + centralized inspection. It assumes the user's own `agate-authenticated`
role, so deploy it **before** `agate-identity` the first time (identity trusts the choke
point's pinned `agate-chokepoint-exec` role by ARN; the role must exist first), then redeploy
identity. Pass `-c auth_role_arn=<AuthenticatedRoleArn> -c spend_table=… -c budget_table=… -c
cognito_discovery_url=… -c cognito_audience=… -c site_url=<cloudfront>`. **audit** (`agate-audit`)
adds the spend/forensic trail.

**Teardown:** `npx cdk destroy agate-web agate-agent agate-data agate-identity` (RETAIN'd buckets/KMS
in `agate-data`/`agate-audit` are kept deliberately — delete them by hand when done).

> **Demo honesty note.** Until a campus IdP is wired, login is whatever OIDC provider you point
> the broker/agent at. The auth path is *real* (RS256/JWKS verified server-side) — there is no
> "paste an unsigned token" shortcut anymore. Tier 0 (Ask) is the proven-clean path; Panel/Analyze
> run on the agent. No resource bills while idle except per-byte storage.

## License

[Apache License 2.0](LICENSE) (see also [`NOTICE`](NOTICE)). Open source by construction — no vendor capture.
