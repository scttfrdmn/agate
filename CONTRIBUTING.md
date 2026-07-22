# Contributing to agate

Thanks for your interest in agate — an open-source, AWS-native GenAI gateway for higher
education. This guide covers how to get set up, the conventions we hold to, and how to land a
change. The authoritative architecture is [`docs/agate-design.md`](docs/agate-design.md); the
coding contract is [`CLAUDE.md`](CLAUDE.md).

## Ground rules

- **NO CLOCKS.** agate has zero standing cost by design — no always-on, bill-by-the-hour
  resources (NAT gateways, OpenSearch, idle containers, interface VPC endpoints, provisioned
  throughput). On-demand / per-request / per-byte only. A change that adds a clock needs a
  named requirement and explicit call-out.
- **Scoped, short-lived credentials only.** No long-lived keys in code or the browser; identity
  is always derived from the verified IdP token, never the request body. Never widen an IAM
  scope "to make it work."
- **Project management lives in GitHub, not in the repo.** No `STATUS.md` / `ROADMAP.md` /
  `TODO.md` / progress files — use Issues, Milestones, and the CHANGELOG.

## Toolchain

- **IaC:** AWS CDK v2, Python bindings (`aws-cdk-lib` ≥ 2.215). Node.js is required (the CDK CLI
  and jsii runtime are npm packages) even though the app is Python.
- **Python 3.12/3.13** for the CDK app and every Lambda. Managed with [`uv`](https://docs.astral.sh/uv/).
- **Go** toolchain for `cli/`.
- **Node.js** for the `web/` SPA (Vite + TypeScript).

## Getting set up

```bash
# Python (CDK app, Lambdas, pure libs)
uv sync

# Web SPA
cd web && npm install && cd ..

# Go CLI
go build ./cli/...
```

## Running the checks

A change isn't done until these pass locally:

```bash
# Python — tests WITH the coverage gate (>= 60%), then lint + format
uv run pytest
uv run ruff check && uv run ruff format

# Web SPA — typecheck, tests, build
cd web && npm run typecheck && npm test && npm run build && cd ..

# Go CLI
gofmt -l ./cli && go vet ./cli/... && go test ./cli/...
```

## Conventions

- **Python:** idiomatic, fully type-hinted; keep pure logic (cost/pricing/ABAC) side-effect-free
  and unit-tested without AWS (mock boto3 with moto or stubs — no live AWS in unit tests).
- **TypeScript (`web/`):** AWS SDK v3 modular clients; no secrets in client code (creds come from
  the runtime identity exchange). The transport adapters implement one interface.
- **Go (`cli/`):** `gofmt` + `go vet`; small, composable, coreutils-style commands.
- **Tests:** maintain ≥ 60% unit-test coverage over our own source; cover load-bearing and
  security-critical paths thoroughly (the claims→scoped-STS broker and the ABAC tag scheme).

## Landing a change

1. **Open or reference an Issue** — units of work live there (`#123` in branches/commits/PRs).
2. **Branch** off `main`; keep diffs small and focused.
3. **Update `CHANGELOG.md`** under `## [Unreleased]` (Keep a Changelog format) — human-written,
   user-facing entries. Don't paste `git log`.
4. **Open a PR** referencing the Issue. CI + review must pass; we squash-merge.
5. Follow **SemVer 2.0.0** for versioning and tags.

## Reporting security issues

Please do **not** open a public Issue for a vulnerability — see [`SECURITY.md`](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the project's license.
