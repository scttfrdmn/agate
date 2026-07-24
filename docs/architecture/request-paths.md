# Request paths

agate is an **identity-native, serverless access layer**: it uses **direct AWS access where the
service and authorization boundaries permit it, and narrow serverless mediation where they
don't**. There isn't one topology — there are four, and they share one ABAC boundary. This page
is the quick reference; the security reasoning is in the [CISO brief](../agate-security-ciso.md).

Every path starts the same way: a person federates their campus identity, the **broker verifies
the IdP token** and exchanges it for **short-lived STS credentials carrying ABAC session tags**
(`agate:tenant`, `agate:scope`, `agate:tier`). Those tags are what fence everything downstream.

## The four paths

| # | Capability | Path | Why not browser-direct? |
|---|------------|------|-------------------------|
| 1 | **CLI / native** model call | scoped creds → **Amazon Bedrock** directly | Nothing in the way — a native caller can sign SigV4 requests to Bedrock with the scoped creds. |
| 2 | **Browser** model call ("Ask") | browser → **chokepoint** (Lambda) → Bedrock | Bedrock's runtime endpoint sends no CORS headers, so a web-origin call is blocked. The chokepoint also enforces the budget and meters spend server-side. |
| 3 | **Scoped retrieval** | browser → **retrieval proxy** (Lambda) → S3 Vectors | The sub-tenant scope filter can't be expressed with IAM conditions alone, so the proxy injects it server-side; the browser never gets vector-query permission. |
| 4 | **Agent** execution | user authority → **AgentCore**-mediated run | Agents run server-side on AgentCore Runtime (scales to zero), acting under a credential derived from the user's authority. |

### 1. CLI / native → Bedrock direct
The elegant case. The `agate` CLI (or any native caller) holds the broker-vended scoped
credentials and calls Bedrock directly. No mediating hop; the ABAC tags on the credential are
the fence.

### 2. Browser "Ask" → chokepoint → Bedrock
The browser cannot call Bedrock directly (no CORS). The **`agate-chokepoint`** Lambda (behind an
AWS_IAM Function URL, SigV4-signed with the scoped creds) is the mediator: it re-derives identity
from the verified token, gates the request against the budget, invokes Bedrock, and returns the
answer plus a cost receipt. This is why `agate-chokepoint` is **required** for browser Ask, not
optional.

### 3. Scoped retrieval → retrieval proxy → S3 Vectors
RAG grounding needs a vector query fenced to the caller's tenant **and** sub-tenant scope (e.g. a
specific course). IAM can fence the tenant but not express the finer scope filter, so the
**retrieval proxy** (an `agate-identity` output) runs the query server-side with the filter
injected from the verified tags. The browser is never granted vector-query permission.

### 4. Agent → AgentCore
Panel/Analyze and future standing agents run on **AgentCore Runtime** (serverless, scales to
zero). The agent acts under a credential derived from the user's authority; the agent spec
compiler is the mechanism for turning declared scopes/tools/budgets into that credential. Much of
this is still a [seam or vision](../product/maturity-matrix.md#agate-agent-research).

## One boundary, four topologies

The point of the design is that all four are fenced by the **same ABAC vocabulary**. Whether the
credential is used directly (path 1), behind a chokepoint (2), by a proxy (3), or by an agent (4),
the tenant/scope/tier tags decide which models it may call and which data it may read. Access
control isn't re-implemented per path — it's the same tag scheme every time.
