# agate documentation

Start here. This page routes you by **task**; the long-form documents below are the deep
reference. If you only read one thing to calibrate what's real today, read the
[maturity matrix](product/maturity-matrix.md).

## By task

| I want to… | Go to |
|------------|-------|
| Understand the idea in 5 minutes | [README](../README.md) → then [Architecture](agate-design.md) §1–3 |
| Know **what's real vs experimental vs vision** | [Maturity matrix](product/maturity-matrix.md) |
| See **how a request actually flows** (web vs CLI vs agent) | [Request paths](architecture/request-paths.md) |
| **Deploy a demo** in one command | [README → Deploying a demo](../README.md#deploying-a-demo) (`scripts/deploy-demo.sh`) |
| Do a real / campus deploy | [README → Deploying a demo](../README.md#deploying-a-demo) (manual sequence) |
| Understand the **security model** | [CISO brief](agate-security-ciso.md) |
| Understand **identity → scoped credentials → ABAC** | [Architecture](agate-design.md) §3, §5 |
| Understand the **cost model / NO CLOCKS** | [README → NO CLOCKS](../README.md#the-governing-principle-no-clocks), [Architecture](agate-design.md) §9 |
| See the **academic interaction model** (Ask/Panel/Analyze) | [Academic interaction model](academic-interaction-model.md) |
| Understand **where agents are going** | [Agent platform vision](agate-agents-vision.md) |
| **Contribute** | [CONTRIBUTING](../CONTRIBUTING.md), [SECURITY](../SECURITY.md) |

## The documents

These are substantial, essay-style references. They are the source of truth; the task table
above tells you which section answers a given question.

- **[agate-design.md](agate-design.md)** — architecture, the source of truth. Identity/ABAC,
  request tiers, cost model, stacks, phases.
- **[agate-security-ciso.md](agate-security-ciso.md)** — the security rationale: why scoped
  short-lived credentials, per-path security properties, shared-responsibility posture.
- **[agate-agents-vision.md](agate-agents-vision.md)** — forward-looking agent platform. Uses
  `[built]` / `[seam]` / `[vision]` labels throughout.
- **[academic-interaction-model.md](academic-interaction-model.md)** — Ask / Panel / Analyze as
  distinct intellectual activities.
- **[product/maturity-matrix.md](product/maturity-matrix.md)** — per-stack/feature status.
- **[architecture/request-paths.md](architecture/request-paths.md)** — the four real request paths.

## The three agates (keep them distinct)

The repo contains three things at once; the maturity matrix draws the lines:

1. **Agate Core** — identity federation, credential brokering, ABAC, model authorization, budget
   enforcement, audit, secure retrieval mediation. *This is what agate fundamentally is.*
2. **Agate Experiences** — Ask, Panel, Analyze, corpus, drafting, authoring, notebooks, rooms, LTI.
3. **Agate Agent Research** — standing authority, agent specs, interop protocols, payments (vision).
