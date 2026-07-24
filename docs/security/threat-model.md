# Threat model & credential lifecycle

A focused threat model for agate's load-bearing path: campus identity → short-lived, scoped AWS
credentials → ABAC-fenced model and data access. The design rationale is in the
[CISO brief](../agate-security-ciso.md); this page states the assets, boundaries, adversaries,
and the credential lifecycle in one place a reviewer can read end-to-end.

## Assets

- **Education records / research data** in the tenant's S3 docs bucket and S3 Vectors indexes.
- **The scoped STS credentials** the broker vends — short-lived, but a live credential is the keys
  to exactly one user's entitlement while it lasts.
- **The campus IdP token** the SPA forwards (an OIDC `id_token` / SAML assertion).
- **Model access + budget** — which Bedrock models a session may call, and the spend it may incur.
- **Audit integrity** — the CloudTrail + invocation-log record of who did what.

## Trust boundaries

| Boundary | Trusted? | Enforced by |
|----------|----------|-------------|
| The browser / SPA | **Untrusted** | Holds only short-lived scoped creds; every authority decision is re-made server-side. Client-supplied tenant/scope/tier are *ignored*. |
| The campus IdP token | Trusted **only after verification** | The broker verifies signature (RS256/JWKS) + issuer/audience before deriving anything. |
| The broker (`agate-identity`) | **Trusted, most critical** | Fails closed; holds no model/data perms itself — only `sts:AssumeRole` on the authenticated role. |
| The chokepoint / retrieval proxy / corpus / memory Lambdas | Trusted mediators | Re-derive identity from the verified token; assume tenant-fenced roles with the session tags. |
| Bedrock / S3 / S3 Vectors | Trusted AWS services | ABAC via `${aws:PrincipalTag/...}` conditions on the vended session tags. |
| Other tenants / users | **Adversarial** | The `agate:tenant` / `agate:scope` tags fence every read/write; cross-tenant access is the primary thing the design prevents. |

## Adversaries & what stops them

- **A logged-in user reaching another tenant's data.** The vended credential carries
  `agate:tenant`/`agate:scope` session tags; bucket/index policies interpolate
  `${aws:PrincipalTag/agate:tenant}`, so a read outside the fence is denied by IAM, not by app
  code. Retrieval's sub-tenant filter is injected server-side (the browser never gets vector-query
  permission).
- **A user escalating their model tier or budget.** `agate:tier` is *derived by the broker* from
  verified claims (not taken from the request), and the chokepoint gates spend server-side against
  the budget table — a client can't widen either.
- **A forged or replayed token.** The broker verifies signature + issuer/audience against the IdP's
  published keys and fails closed on any mismatch; an expired token is rejected (short id_token
  lifetime). No credential is vended on any validation error.
- **A stolen scoped credential.** Blast radius is one user's entitlement for ≤ the session lifetime
  (**15 minutes**, `AGATE_SESSION_DURATION_SECONDS`), versus a standing privileged app proxy whose
  compromise exposes everyone. This smaller-blast-radius argument is the core security thesis.
- **Client tampering (skipping the chokepoint, calling Bedrock directly).** A browser *can't* call
  Bedrock directly (no CORS); a native caller with scoped creds still only gets its own
  tag-fenced entitlement. Authority never depends on the client taking the "intended" path.
- **Network position (optional).** The broker supports a source-IP allowlist (CIDRs) enforced in
  the handler, since API Gateway HTTP APIs have no resource policy.

## Credential lifecycle

```
  Campus IdP                Broker (agate-identity)                 AWS STS
  ----------                -----------------------                 -------
  user authenticates
      │  id_token (OIDC) / SAML assertion
      ▼
  SPA ──forwards token──▶  1. verify signature (RS256/JWKS)
                             + issuer/audience         ──fail──▶ 403, NO credential
                           2. claims_to_tags():
                              derive agate:tenant / scope /
                              affiliation / TIER  (pure)
                           3. sts:AssumeRole(authenticated_role,
                              Tags=[agate:*], Duration=900s) ─────▶ short-lived creds
                                                                     narrowed by those tags
      ◀───────────────── scoped credentials (≤15 min) ────────────────┘
      │
      ▼  used directly (CLI) OR via chokepoint / retrieval proxy / agent
   every downstream call is fenced by ${aws:PrincipalTag/agate:*}
```

Why **`AssumeRole`** (not `AssumeRoleWithWebIdentity`): the **tier is derived**, and
`AssumeRoleWithWebIdentity` can only echo principal-tag claims already in the token — it can't
carry a value the broker computes. `AssumeRole`'s `Tags` parameter can. That is the entire reason
the broker exists rather than a direct Cognito→STS exchange.

## Explicit non-goals / shared responsibility

- agate does not make an institution FERPA-compliant on its own — it makes the controls
  enforceable. Compliance still depends on institutional configuration, data classification,
  contracts, and operational practice (see [design §4](../agate-design.md)).
- Endpoint security of the user's device, campus IdP hygiene, and AWS account-level controls (SCPs,
  root-account protection) are out of scope — assumed to be the institution's responsibility.
- Denial-of-service and cost-exhaustion are bounded by budgets, not eliminated.
