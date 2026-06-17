# agate

An open-source, **AWS-native GenAI gateway for higher education** with **zero standing cost**.
A person federates their campus identity, receives short-lived credentials scoped to exactly
the models and documents they're entitled to, and the browser talks to Amazon Bedrock and
S3 Vectors directly — with both the talking and the documents fenced by the *same* boundary.

- **Architecture (source of truth):** [`docs/agate-design.md`](docs/agate-design.md)
- **Agent platform vision (where it goes):** [`docs/agate-agents-vision.md`](docs/agate-agents-vision.md) — agate as the governance layer over the open agent stack; *agenkit builds the agent, agate governs it* ([agenkit.dev](https://agenkit.dev))
- **Security rationale (CISO memo):** [`docs/agate-security-ciso.md`](docs/agate-security-ciso.md)
- **Coding contract:** [`CLAUDE.md`](CLAUDE.md)

> Project, repo, package, and CLI binary are all **`agate`** (named for agate, a banded form
> of bedrock). Session-tag namespace `agate:`; docs bucket prefix `agate-docs`. The name is
> still provisional ("for now").

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
# Python / CDK
cd infra
uv sync                       # install CDK + deps into .venv
uv run pytest                 # pure-logic unit tests (no AWS)
uv run ruff check && uv run ruff format

# CDK synth (needs Node for the aws-cdk CLI + jsii)
npx cdk synth

# Go CLI
cd ../cli && go build ./... && go test ./...
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

The eight stacks are independent; deploy only what a given demo needs. The Tier 0 path
(`agate-identity`) is $0-idle and the safest first deploy; the data/agent/web stacks add
storage + a container.

**0. Refresh per-model pricing (optional, recommended).** Bake authoritative Bedrock
list rates into the cost engine before deploying the metering stacks. Read-only against
AWS (`pricing:GetProducts`, us-east-1); without it the engine uses live-verified hard
defaults, so this is a refresh, not a prerequisite.
```bash
uv run python -m cost.pricelist --out cost/model_rates.json   # generated artifact, gitignored
```
The meter/chokepoint Lambdas load `cost/model_rates.json` automatically (it ships inside
the bundled `cost` package) — no env var, no runtime Price List call (NO CLOCKS).

**1. Identity (the crux — Tier 0).**
```bash
npx cdk deploy agate-identity            # Cognito Identity Pool, broker Lambda, ABAC role
```
Real login needs OIDC config on the broker (it verifies the IdP token — no placeholder):
set `AGATE_OIDC_JWKS_URL`, `AGATE_OIDC_ISSUER`, `AGATE_OIDC_AUDIENCE` for your campus IdP (or a
demo Cognito User Pool / any OIDC provider). Without them the broker fails closed.

**1a. Demo IdP (only if you have no campus IdP to point at).**
```bash
npx cdk deploy agate-demo-idp            # throwaway Cognito User Pool that issues real RS256 JWTs
```
The stack outputs `OidcIssuer`, `OidcJwksUrl`, and `OidcAudience` — set those as
`AGATE_OIDC_ISSUER` / `AGATE_OIDC_JWKS_URL` / `AGATE_OIDC_AUDIENCE` on the broker (and the agent's
`cognito_discovery_url` / `cognito_audience`). Create a demo user and set their
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
  VITE_RETRIEVAL_URL=<retrieval-url> \
  VITE_AGENT_RUNTIME_ARN=<runtime-arn> \
  VITE_DRAFTING_URL=<drafting-url> \
  VITE_DEPLOY_URL=<deploy-url> \
  VITE_AUTHORING_URL=<authoring-url> \
  VITE_ROOMS_URL=<rooms-url> npm run build && cd ..
npx cdk deploy agate-web                 # publishes web/dist to S3 + CloudFront
```
The `agate-web` output `SiteUrl` is the demo URL. `VITE_RETRIEVAL_URL` is the
`agate-identity` output `RetrievalUrl` — the broker-proxied vector retriever that
enforces sub-tenant scope (#84); omit it to disable RAG grounding. `VITE_DRAFTING_URL`
is the `agate-drafting` output `DraftingUrl` (#118b) — the natural-language "Draft an
agent" screen; omit it to hide that screen. `VITE_DEPLOY_URL` is the `agate-deploy`
output `DeployUrl` (#118) — the confirm-and-create action; omit it and drafts render
but the confirm button stays inert. `VITE_AUTHORING_URL` is the `agate-authoring`
output `AuthoringUrl` (#117) — the visual "Build an agent" screen (bounded menu +
form); omit it to hide that screen. `VITE_ROOMS_URL` is the `agate-rooms` output
`RoomsUrl` (#116) — the collaborative "Rooms" screen (polling transport); omit it to
hide that screen.

**5. Optional Tier 1** (`agate-chokepoint`) and **audit** (`agate-audit`) only if the demo needs
exact pre-call caps or the spend/forensic trail.

**Teardown:** `npx cdk destroy agate-web agate-agent agate-data agate-identity` (RETAIN'd buckets/KMS
in `agate-data`/`agate-audit` are kept deliberately — delete them by hand when done).

> **Demo honesty note.** Until a campus IdP is wired, login is whatever OIDC provider you point
> the broker/agent at. The auth path is *real* (RS256/JWKS verified server-side) — there is no
> "paste an unsigned token" shortcut anymore. Tier 0 (Ask) is the proven-clean path; Panel/Analyze
> run on the agent. No resource bills while idle except per-byte storage.

## License

See [`LICENSE`](LICENSE) (to be added). Open source by construction — no vendor capture.
