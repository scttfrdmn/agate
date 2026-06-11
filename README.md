# aws-genai-gateway (`agg`)

An open-source, **AWS-native GenAI gateway for higher education** with **zero standing cost**.
A person federates their campus identity, receives short-lived credentials scoped to exactly
the models and documents they're entitled to, and the browser talks to Amazon Bedrock and
S3 Vectors directly — with both the talking and the documents fenced by the *same* boundary.

- **Architecture (source of truth):** [`docs/aws-genai-gateway-design.md`](docs/aws-genai-gateway-design.md)
- **Security rationale (CISO memo):** [`docs/aws-genai-gateway-security-ciso.md`](docs/aws-genai-gateway-security-ciso.md)
- **Coding contract:** [`CLAUDE.md`](CLAUDE.md)

> Names are provisional ("for now"): project/repo/package `aws-genai-gateway`; CLI binary and
> short handle `agg`; session-tag namespace `agg:`; docs bucket prefix `agg-docs`.

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
2. **The ABAC tag scheme** — one `agg:` tag scheme that simultaneously governs which Bedrock
   models a session may invoke and which S3 prefixes / S3 Vectors indexes it may read.

## Repository layout

```
infra/    AWS CDK v2 (aws-cdk-lib, Python 3.13). One app, small focused stacks.
          stacks/identity.py is the load-bearing Phase 1 stack.
          lambda/broker/      per-request credential-vending broker (scales to zero).
web/      Static SPA (Vite + TypeScript). Three swappable transport adapters.
cli/      `agg` admin CLI (Go).
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
| Go | 1.22+ | The `agg` CLI in `cli/`. |

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

## License

See [`LICENSE`](LICENSE) (to be added). Open source by construction — no vendor capture.
