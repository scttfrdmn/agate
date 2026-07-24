# Regions & AWS service support

agate depends on several AWS capabilities whose availability is **region-specific**. This page
lists what the default path needs, the assumptions the architecture makes about service behavior,
and how to choose a region. **us-east-1** is the current reference/development region.

> Availability changes as AWS expands services to new regions. Check the AWS Region Table and each
> service's docs for your target region before deploying; treat the "default path" column as the
> minimum, not the whole story.

## What each path needs

| Path / feature | AWS services | Region-sensitive? |
|----------------|--------------|-------------------|
| Identity / broker (core) | Cognito (demo IdP), STS, Lambda, API Gateway (HTTP API) | STS/Lambda/API GW are broadly available; Cognito Hosted UI is the demo-only piece. |
| Retrieval / RAG (core) | **S3 Vectors**, **Bedrock** embeddings (Titan), Lambda | **Yes** — S3 Vectors and the specific Bedrock embedding model must exist in-region. |
| Browser "Ask" (core) | **Bedrock** runtime (Converse), Lambda (chokepoint) | **Yes** — the routed model set (incl. the oss/gpt tier) must be available in-region. |
| Web hosting | S3, CloudFront | CloudFront is global; the bucket is regional. |
| Audit / spend | CloudTrail, DynamoDB, S3 | Broadly available. |
| **Pricing refresh** (optional) | Bedrock **Price List API** | **us-east-1 ONLY** — the Price List API is only in us-east-1 (see below). |
| Agents (experimental) | **Bedrock AgentCore** (Runtime, Gateway, Code Interpreter, Memory) | **Yes** — AgentCore has limited regional availability; check before enabling. |
| Guardrails (experimental) | **Bedrock Guardrails** | **Yes** — region-dependent. |

## Service-behavior assumptions (why the architecture is shaped this way)

These are load-bearing assumptions, not incidental — they're why the request paths look the way
they do (see [request paths](../architecture/request-paths.md)):

- **Bedrock's runtime endpoint sends no browser CORS headers.** A web-origin call to Bedrock is
  blocked, so browser "Ask" *must* go through the `agate-chokepoint` Lambda. A CLI/native caller
  is unaffected and calls Bedrock directly.
- **IAM cannot express the S3 Vectors sub-tenant scope filter.** IAM conditions can fence the
  tenant prefix but not the finer in-tenant scope (e.g. a specific course), so scoped retrieval
  goes through the server-side retrieval proxy, which injects the filter from the verified tags.
- **The Bedrock Price List API is us-east-1 only**, and uses the `AmazonBedrockFoundationModels`
  service code (not `AmazonBedrock`); usagetype prefixes are regional (`USW2`, not `us-west-2`).
  The cost engine calls it only during an optional pricing refresh and otherwise uses cached +
  hard-default rates, so this does **not** force the whole deployment into us-east-1.
- **Model IDs and the routed model set are region-scoped.** A model pinned or auto-routed to must
  be enabled in your account *and* available in your region; the entitlement/router tables assume
  the reference set. Confirm model access in the Bedrock console for your region.

## Choosing a region

1. Start from where **Bedrock** (with the models you need) and **S3 Vectors** are both available —
   these gate the core RAG + Ask path.
2. If you'll use **AgentCore** (Panel/Analyze, agents) or **Guardrails**, confirm those too; they
   have narrower availability and may constrain the choice.
3. Data-residency: everything except the optional us-east-1 pricing call stays in your chosen
   region and account, which is the point — inference and records stay in-boundary.
4. When unsure, **us-east-1** is the broadest and is agate's reference region.
