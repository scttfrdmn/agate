# Maturity matrix

Three versions of agate are visible at once in this repo, and it helps to keep them distinct:

1. **the minimal identity-native gateway** — the load-bearing core;
2. **the academic GenAI application** that actually exists on top of it;
3. **the future institutional agent environment** it's growing toward.

This page states, per stack and per user-visible feature, how settled each is. It uses the same
vocabulary as the [agents vision](../agate-agents-vision.md):

- **Available** — deployed and exercised; safe to build on.
- **Experimental** — works, but the shape may change; deploy behind a named requirement.
- **Seam** — an architectural boundary is in place, but the capability behind it is thin/partial.
- **Vision** — designed, not built.

> The distinction the review asked for: only **Agate Core** defines what agate fundamentally
> *is*. Experiences and Agent Research live in the same repo but are explicitly downstream of it.

## Agate Core

Identity federation, credential brokering, ABAC, model authorization, budget enforcement, audit
context, and secure retrieval mediation. This is the product.

| Stack | Maturity | Notes |
|-------|----------|-------|
| `agate-identity` | **Available** | The crux: claims→scoped-STS broker + ABAC session tags. Fail-closed; unit-tested (exact tags, role-session attribution, bounded STS). |
| `agate-data` | **Available** | Docs bucket (per-tenant prefix) + S3 Vectors index + embed-on-upload ingest. |
| `agate-audit` | **Available** | CloudTrail + spend/budget tables; authoritative spend computed server-side from Bedrock invocation logs. |
| `agate-chokepoint` | **Available** | Tier-1 metered/gated Bedrock proxy. **Required** for browser "Ask" (Bedrock's runtime has no browser CORS). Auto model routing. |
| Retrieval proxy (`agate-identity` output) | **Available** | Broker-proxied S3 Vectors query that injects the sub-tenant scope filter server-side (IAM can't express it alone). |
| `agate-governance` | **Experimental** | Bedrock Guardrails + AgentCore policy engine; optional but recommended. |

## Agate Experiences

The academic application: how people actually use the gateway. Ask / Panel / Analyze, corpora,
authoring, notebooks, rooms, LTI.

| Feature / Stack | Maturity | Notes |
|-----------------|----------|-------|
| **Ask** (Tier-0 chat) | **Available** | Browser chat via the chokepoint; RAG grounding, citations, cost receipt, auto-routing, follow-ups. |
| **Panel / Analyze** (agent modes) | **Experimental** | Run on the AgentCore path; event-stream artifacts. Depends on `agate-agent`. |
| **Corpus** (`agate-corpus`) | **Available** | Upload + browse your own in-scope documents; tenant/scope-fenced. |
| **Notebooks** | **Available** | Chat⇄Notebook view; prompt + client-side Python code cells (pyodide, self-hosted incl. numpy/pandas/matplotlib), cross-cell refs with cost-aware reactivity, save/open to the corpus. |
| **Memory** (`agate-memory`) | **Experimental** | Opt-in, **billable-not-$0-idle** AgentCore Memory; cross-session recall. Off by default. |
| **Drafting** (`agate-drafting`) | **Experimental** | Natural-language "draft an agent" → bounded plan. |
| **Authoring** (`agate-authoring`) | **Experimental** | Graphical "build an agent" (bounded menu → compiled spec). |
| **Deploy-on-confirm** (`agate-deploy`) | **Experimental** | Creates an agent from a drafted/authored spec. |
| **Rooms** (`agate-rooms`) | **Experimental** | Collaborative rooms; reach = server-enforced intersection of members. Polling transport ($0-idle). |
| **Admin console** (`agate-admin`) | **Experimental** | Governed-access usage/spend console; the API is the gate (a non-admin session gets 403). |
| **LTI 1.3** (`agate-lti`) | **Experimental** | Tool-provider stack for LMS launch + roster (NRPS). |
| **Demo IdP** (`agate-demo-idp`) | **Available** (demo only) | Throwaway Cognito pool for a self-contained demo when there's no campus IdP. Not for production. |

## Agate Agent Research

Standing authority, agent specifications, interoperability protocols, payments, and future
institutional agent infrastructure. See the [agents vision](../agate-agents-vision.md).

| Capability | Maturity | Notes |
|------------|----------|-------|
| `agate-agent` (AgentCore Runtime + Code Interpreter) | **Seam** | The runtime + gateway are provisioned; several outputs are still PLACEHOLDER (no IdP wired to the gateway, no connectors/OAuth). Reference Strands agent ships; the container is framework-agnostic. |
| Agent spec → compiler → scoped credentials | **Seam** | The most important long-term idea: scopes/tools/budgets/auth as reviewable config that compiles to enforceable credentials. Partially built via drafting/authoring/deploy. |
| Standing agents / delegated authority | **Vision** | Designed in the agents doc; not built. |
| Agent interop (MCP / A2A / AG-UI) | **Vision** | Architectural seams described; not built. |
| Agent payments (AP2 / x402) | **Vision** | Design only. |

## How this maps to deployment

- The **smallest coherent demo** is Agate Core's browser path: `agate-identity` + `agate-data` +
  `agate-chokepoint` + `agate-web` (+ `agate-demo-idp` if you have no campus IdP). See the
  deploy-demo script / quickstart.
- Everything under **Experiences** and **Agent Research** is independently deployable and **off
  unless you deploy its stack and set the matching `VITE_*` variable** — the SPA hides a screen
  whose endpoint isn't configured.
