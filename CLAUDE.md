# CLAUDE.md

Operating instructions for Claude Code in this repo. **Coding conventions and guardrails only.**
Project management does not live here — it lives in GitHub (see *Project management*).

## What this is

`agg` (repo / package `aws-genai-gateway`) is an open-source, AWS-native GenAI gateway for higher
education. The **architecture spec is `docs/aws-genai-gateway-design.md`** — treat it as the source
of truth; this file is the coding contract on top of it. Security rationale is
`docs/aws-genai-gateway-security-ciso.md`.

> **Names (provisional, "for now").** Repo / package: `aws-genai-gateway`. CLI binary + short handle:
> `agg`. Both are still swappable via a global rename — keep them in one or two obvious config/constant
> spots; don't scatter literals.

## Golden rules (do not violate)

1. **NO CLOCKS.** Never add a resource that bills by the wall-clock hour while idle: no NAT gateway,
   no OpenSearch (Serverless or managed), no Bedrock provisioned throughput, no always-on container
   (Fargate/ALB/RDS/ElastiCache), no interface VPC endpoints — unless a *named* requirement forces it
   and you say so explicitly. On-demand / per-request / per-byte only. (design §1, §9, §14)
2. **PM lives in GitHub, never in files.** Do not create status, plan, roadmap, progress, TODO, or
   summary documents. (see *Project management*)
3. **Spend care on the two load-bearing parts:** the claims→scoped-STS broker and the ABAC
   session-tag scheme (design §3.1, §13.1). Everything else is assembly.
4. **No privileged credentials in code or in the browser.** Scoped, short-lived STS only.
   (design §5, security memo §5)

## Toolchain (pinned — don't guess)

- **IaC:** AWS **CDK v2** (`aws-cdk-lib`, pin ≥ 2.215; there is no v3), **Python** binding — not TypeScript.
- **Python 3.12 or 3.13** for the CDK app and *every* Lambda runtime. **Never 3.9** (EOL Oct 2025).
- **Node.js is required** even though IaC is Python: the `aws-cdk` CLI is an npm package and the Python
  bindings call a Node jsii runtime under the hood. Dev boxes and CI need Node present.
- **`uv`** for Python envs/deps. **Go** toolchain for `cli/`.
- New Bedrock / AgentCore / S3-Vectors resources: use L1 `Cfn*` constructs where no L2 exists yet.

## Architecture guardrails

- **Default to Tier 0**: static SPA → browser-direct Bedrock Converse + S3 Vectors via scoped Cognito
  creds. Add Tier 1 (thin Lambda) or Tier 2 (LiteLLM) only under a named requirement. (design §2)
- **Agents** run on AgentCore Runtime (serverless, scales to zero). Ship **Strands** as the reference
  agent but keep the agent a framework-agnostic container. Don't put AgentCore on the plain-chat path.
  (design §13.7)
- **Do NOT add to the core:** LangChain, LlamaIndex, LiteLLM (Tier-2 escape hatch only), or any OSS
  vector DB (Chroma/Qdrant/pgvector). Use Bedrock Converse, Bedrock Knowledge Bases, **S3 Vectors**,
  Bedrock Guardrails, and AgentCore instead. (design §3)
- **One ABAC tag scheme governs both model access and data scope.** Don't fork them.

## Code style

**Python**
- Write **idiomatic, Pythonic** code — clear names, small functions, standard idioms over cleverness,
  full type hints. Prefer the obvious construct a Python reader expects.
- Lint + format with **ruff** (the standard Python linter/formatter as of June 2026; if the toolchain
  moves on, use whatever the current standard is). `ruff check` and `ruff format` must be clean before
  a change is done.
- Keep pure logic pure: `CostMeter` / `pricing` are side-effect-free and unit-tested without AWS.
  Don't couple cost math to boto3 calls.
- `pytest`; mock boto3 (moto or stubs). No live AWS in unit tests.

**Go (`cli/`)**
- `gofmt` + `go vet`. Small, composable commands named like Unix utilities (lowercase, verb-first).

**TypeScript (`web/`)**
- AWS SDK **v3** modular clients. The three transport adapters (`bedrock.ts` / `openai.ts` /
  `agentcore.ts`) implement one interface — switching tiers is config, not a rewrite.
- No secrets in client code; creds come from the Cognito identity exchange at runtime.

**Naming:** small, lowercase, descriptive, coreutils-style. Let modules be what they are.

## Common commands

- `uv sync` — install/refresh Python deps. `uv run pytest` — tests **with the coverage gate**.
  `uv run ruff check && uv run ruff format` — lint + format (must be clean).
- `npx cdk synth` / `npx cdk deploy <stack>` — synth/deploy (uses the pinned `aws-cdk-lib`).
- `go test ./...` / `go build ./cli/...`.

## Testing

- Unit-test all pure logic — cost, pricing, ABAC claim→tag translation, LTI token handling — with no
  AWS dependency.
- **Maintain ≥ 60% unit-test coverage** over our own source (the pure libs and Lambda handlers; CDK
  stacks are exercised by `cdk synth`, not unit tests). The bar is a floor, not a target — cover the
  load-bearing and security-critical paths thoroughly, and add tests as appropriate, not just to move
  the number. The gate is wired into `pytest` (`--cov-fail-under=60`); a plain `uv run pytest` enforces
  it. The TS suite (`vitest`) covers the SPA's pure logic in the same spirit.
- A change isn't done until its tests pass, coverage holds, ruff is clean, **and** the changelog is
  updated.

## Security

- Scoped STS sessions only. Never embed long-lived keys. Never widen an IAM scope "to make it work."
- Secrets via env / SSM Parameter Store / Secrets Manager — never committed. `.env` is git-ignored.
- Authoritative spend is computed server-side from Bedrock invocation logs — never trusted from the
  client. (design §7.2)

## Versioning — SemVer 2.0.0

Follow Semantic Versioning 2.0.0 (https://semver.org).

- **MAJOR.MINOR.PATCH**: MAJOR = incompatible API/contract change; MINOR = backward-compatible feature;
  PATCH = backward-compatible fix.
- Pre-1.0 (`0.y.z`): anything may change; bump MINOR for breaking changes, PATCH otherwise.
- Tag releases `vX.Y.Z`. Pin dependencies — don't float.

## Changelog — Keep a Changelog 1.1.0

Maintain `CHANGELOG.md` per https://keepachangelog.com (1.1.0).

- Top section is `## [Unreleased]`. On release, rename it to `## [X.Y.Z] - YYYY-MM-DD` and start a fresh
  `Unreleased`.
- Group entries under **Added, Changed, Deprecated, Removed, Fixed, Security**.
- Entries are human-written and user-facing. **Do not paste `git log`.** Keep compare links at the bottom.
- Every PR that changes behavior updates `Unreleased`.

## Project management — GitHub only

All planning and tracking lives in GitHub. **Never write it into the repo as files.**

- **Issues** = units of work (bugs, features, tasks). Reference them in branches/commits/PRs (`#123`).
- **Milestones** = releases/phases (mirror the build phases, design §12).
- **Projects** = the board / roadmap view.
- **Labels** = categorization (area, type, priority).
- **Forbidden in-repo:** `STATUS.md`, `ROADMAP.md`, `TODO.md`, `PLAN.md`, progress reports,
  "summary of changes" files, or any standing status doc. If tempted to write one, open or update an
  **Issue**, or add to the **CHANGELOG** — never a file.

## The only docs that live in the repo

Code, tests, `README.md`, `CHANGELOG.md`, `CLAUDE.md`, and the design/security docs under `docs/`.
Nothing status-shaped.

## Definition of done

Code + passing tests + a `CHANGELOG` *Unreleased* entry + the relevant **Issue** referenced. No status
file. Keep diffs small and focused — small stacks, small PRs.
