# agate — Security Justification for Browser-Direct Model Access
## A memo for the CISO / security architecture review

> **Audience:** Chief Information Security Officer, security architects, FERPA/privacy
> officer. **Purpose:** justify the Tier 0 architecture — a static client that calls
> Amazon Bedrock and Amazon S3 Vectors directly using short-lived, scoped credentials
> — and preempt the objections a security review will (correctly) raise.
> Project / repo / package: **`agate`** (provisional, "for now"). Short handle / CLI: **`agate`**.

---

## 1. Executive summary

`agate`'s default ("Tier 0") design has the user's browser call Amazon Bedrock and S3
Vectors **directly**, using temporary AWS credentials vended by Amazon Cognito after
the user authenticates through the campus identity provider. There is **no proxy
server** in the request path and **no long-lived secret** anywhere in the client.

The instinctive objection — "you put AWS credentials in a browser" — dissolves on
inspection of *what those credentials can do*:

- They are **short-lived** STS session credentials (on the order of an hour,
  configurable), not static keys. Compromise yields a time-boxed session, not a
  standing credential.
- They are **scoped to the authenticated user's own entitlement** via the IAM role and
  ABAC session tags: invoke only the models that user's tier permits, read only the
  S3 prefix / vector index for that user's tenant and courses.
- Therefore the **blast radius of a stolen credential equals the blast radius of the
  user it belongs to** — which is the irreducible floor of *any* system. The browser
  holds no authority the user does not already have through the UI.

The design's security does **not depend on the client behaving.** We assume the client
is hostile. The authorization decision is enforced by **AWS IAM at the resource**, not
by client code, and the client cannot widen its own scope.

Counterintuitively, this is a **smaller** aggregate credential-theft surface than the
conventional proxy design (§5).

---

## 2. Scope of this memo

This covers **Tier 0** — browser-direct access — which is the default and the basis of
`agate`'s cost and sovereignty properties. Two optional tiers exist for institutions with
stricter requirements (a server-side choke point for hard pre-spend budget cutoffs and
centralized inspection; a managed gateway container). They reduce some residual risks
below at the cost of standing infrastructure. This memo defends the *default*; the
optional tiers are strictly additive controls, not prerequisites. Agentic workloads run
on AgentCore (server-side, not browser-direct); their containment is covered in §6.1.

---

## 3. Trust model & boundaries

```
  Campus IdP            Cognito Identity Pool        Browser (UNTRUSTED)      AWS resources
  (TRUSTED)             (validation + vend)          holds temp scoped creds  (enforce IAM/ABAC)
      │   SAML/OIDC          │                              │                       │
      │  assertion           │  derives STS creds           │  SigV4 request        │
      └─────────────────────►│  scoped by role+tags ───────►│──────────────────────►│
                             │                              │   InvokeModel /        │  ALLOW/DENY
                             │                              │   S3 Vectors query     │  decided here
```

- **Trusted:** the campus IdP (authoritative for identity and affiliation) and the AWS
  control plane (Cognito, STS, IAM).
- **Untrusted:** the browser/client. It is treated as potentially compromised.
- **Enforcement point:** AWS IAM, evaluated **at the resource** on every call. The
  client presents credentials; AWS decides allow/deny. Client code is not part of the
  authorization decision and cannot be relied on or bypassed to change it.

**What the browser is *not* trusted to do:** nothing beyond what its resource-side IAM
scope grants. There is no client-side authorization logic to subvert.

---

## 4. The central argument — bounded blast radius

The credentials in the browser are produced by this chain:

1. User authenticates to the **campus IdP** (Shibboleth/InCommon, Entra, or Okta).
2. **Cognito Identity Pool** validates the federated assertion and, server-side, mints
   **temporary STS credentials** for an authenticated IAM role.
3. The role's effective permissions are **narrowed by session tags** derived from IdP
   claims: `agate:affiliation`, `agate:tenant`, `agate:courses`, `agate:tier`.
4. The browser uses those creds (SigV4) to call only what the scope permits.

Consequences a reviewer should verify:

- **No privilege beyond the user.** The creds can do exactly what the user is
  authorized to do — invoke their tier's models, read their tenant's data. Nothing
  more. There is no path to escalate.
- **No standing secret.** No IAM user, no static access key, no API token in the client.
- **Time-boxed.** Sessions expire (≈1h, configurable) and refresh against the live IdP
  session. Revoking the IdP session ends access on refresh.
- **Fully attributable.** Every Bedrock and S3 call is logged (CloudTrail, Bedrock
  invocation logging) against the Cognito identity — per-user forensic trail.

The worst outcome of a stolen Tier 0 credential is that an attacker does, for under an
hour, what that one user could already do: consume that user's own token budget and
read that user's own in-scope data. It is **not** an escalation, **not** cross-tenant,
**not** infrastructure access.

---

## 5. Why this is *safer* than a privileged proxy

The conventional "secure" pattern — a server-side proxy that holds AWS credentials and
makes calls on behalf of all users — concentrates risk:

| Property | Privileged proxy | `agate` Tier 0 (browser-direct) |
|---|---|---|
| Credential power | One credential able to act for **all** users/tenants | Each session holds only **one user's** minimal scope |
| Value as a target | High — single point, total compromise | Low — no super-credential exists |
| Compromise blast radius | **All tenants** (catastrophic, FERPA-wide) | **One user**, time-boxed |
| Standing attack surface | Always-on service, patch treadmill, network exposure | No always-on service in the path |
| Authorization locus | Application code (bugs can leak across tenants) | AWS IAM/ABAC (below the app) |

Removing the super-credential removes the highest-value target. The browser-direct
model distributes authority into many minimal, expiring, self-scoped sessions instead
of concentrating it in one privileged service.

---

## 6. Tenant isolation (the FERPA-critical control)

Cross-tenant data leakage — a CHEM-101 student reading PSYCH-200 records — is the
nightmare case. In `agate` it is prevented **structurally, below the application**:

- Each tenant's documents live under a dedicated S3 prefix; each tenant has its own
  S3 Vectors index with its **own customer-managed KMS key**.
- The session's IAM scope permits S3/vector reads **only** for the `agate:tenant` /
  `agate:courses` carried in its tags. Cross-tenant reads are **denied by the credential**.
- Because isolation is enforced at the AWS authorization layer, **an application bug
  cannot leak across tenants** — the deny is in IAM, not in app logic that could be
  bypassed. This is a stronger guarantee than app-enforced multi-tenancy.

Course enrollment driving `agate:courses` comes from the LMS via LTI 1.3 NRPS (the
authoritative roster), so retrieval scope tracks actual enrollment.

### 6.1 Agent path containment (AgentCore)

Agentic workloads (multi-step, tool-using, code-executing) do **not** run browser-direct;
they run server-side on **Amazon Bedrock AgentCore**, which is a *stronger* containment
posture than the chat path, not a weaker one:

- **Per-session microVM isolation.** Each agent session runs in its own microVM that is
  destroyed at session end. One user's agent run cannot observe another's.
- **Identity-bound.** AgentCore Identity validates the invoking user (inbound, via Cognito
  → the campus IdP) and carries the user's `agate:tenant`/`agate:courses` into the session, so
  the agent acts only within that user's scope.
- **Every tool call is authorized.** AgentCore Policy (Cedar) gates each tool/action
  against the user's entitlements; tools behind AgentCore Gateway enforce the same
  tenant/course scope as the chat path (they assume the user's scoped role or apply ABAC).
  Web/tool access an institution wants blocked is denied by policy, not by trusting the
  agent — the demonstrated "Cedar policy denies web_fetch" pattern.
- **Code runs sandboxed.** AgentCore Code Interpreter executes in an isolated sandbox, not
  in any shared or privileged context.
- **Fully traced.** AgentCore Observability (CloudWatch/OpenTelemetry) records the agent's
  every step, model call, and tool invocation for audit.

The same FERPA guarantee holds: tenant isolation is enforced by AgentCore Policy + scoped
tools (below the agent's own logic), so a flaw in agent reasoning cannot breach tenant
boundaries.

---

## 7. Threat scenarios & containment

| Threat | Containment |
|---|---|
| **XSS / malicious extension steals temp creds** | Scoped to one user, expires <1h, cannot cross tenants or escalate; CSP + Subresource Integrity + no third-party script in SPA; all use logged + attributable |
| **Stolen creds replayed outside the SPA (curl/script)** | Same scope ceiling — the user's own budget/data; detectable via CloudTrail anomaly; (Tier 1 adds hard pre-spend cutoff) |
| **Authenticated user abuses their own access** | Bounded by per-user/tenant budget (cost-allocation tags; Tier 1 hard cutoff if required); bounded and detectable |
| **Client misreports its own spend (Tier 0 meter)** | The enforcement number is computed server-side from Bedrock invocation logging × Price List rates, **not** from the client; the client's live cost display is non-authoritative UX; overrun bounded to the user's *own* budget, never cross-tenant |
| **Cross-tenant exfiltration** | Denied by IAM/ABAC at the resource; per-index CMK; not reachable regardless of client behavior |
| **Prompt injection** | Can shape response *content within the user's own scope*; cannot cross the authorization boundary (retrieval creds won't permit out-of-scope reads); Bedrock Guardrails for PII/content |
| **Model provider retains/trains on records** | Bedrock-hosted models (OpenAI GPT-5.x, Anthropic, Llama, Mistral, gpt-oss) — inference stays in-account/in-region, no provider-side retention |
| **Identity spoofing / assertion replay** | Federation validated by Cognito per SAML/OIDC; STS creds derived server-side, not client-forgeable; standard nonce/assertion checks |
| **Broker vends over-broad creds (mis-scoping)** | The one genuinely dangerous bug — see Residual Risks; this path is the priority for review/pen-test |

---

## 8. Compliance mapping

**FERPA.** The institution is the data controller throughout. Education records remain
in the institution's own AWS account; inference runs in-account and in-region with no
third-party retention; tenant isolation is enforced at the IAM layer; access is fully
audited and attributable. No record is exposed to a public AI service.

**HIPAA (academic medical center).** The same architecture operates under the AWS
Business Associate Agreement; Amazon Bedrock is HIPAA-eligible; PHI never leaves the
account. (Internet2 NET+ AWS already provides BAA terms.)

**Data residency / sovereignty.** Region pinning constrains where records and inference
live; AWS GovCloud (US) is available where required, including for the OpenAI GPT-OSS
and frontier models now offered on Bedrock.

---

## 9. Operational controls (recommended baseline)

- **Short STS TTL** with refresh tied to the live IdP session; IdP logout ends access.
- **CloudTrail** + **Bedrock invocation logging** to a restricted-access S3 log bucket
  — per-identity forensic trail and chargeback source.
- **CSP + SRI** on the SPA; no third-party scripts; no secrets in client storage.
- **Cost-allocation tags** per tenant/user; budget alarms; (Tier 1) hard pre-spend caps.
- **Bedrock Guardrails** for PII redaction and content filtering.
- Optional **GuardDuty** / anomaly detection on the credential and Bedrock usage paths.
- **KMS CMK per tenant** on S3 Vectors indexes; key-policy review as part of onboarding.

---

## 10. Residual risks (stated plainly)

1. **Credential-vending mis-scope.** If the broker maps IdP claims to session tags
   incorrectly, it could vend over-broad credentials. This is the single most important
   thing to review. **Recommendation:** independent review + penetration test of the
   identity→scoped-STS path before production; treat it as the security-critical core.
2. **Within-scope abuse.** An authenticated user can exhaust their own budget or query
   their own data via direct calls. Bounded and detectable; Tier 1 adds hard cutoff.
3. **Client-side compromise.** XSS/extension can act as the user for the session's life.
   Bounded to one user, time-boxed; mitigated by SPA hardening and short TTL.
4. **Spend overrun is bounded, not eliminated, in Tier 0.** The number that enforces the
   **soft cap** is computed **server-side from Bedrock invocation logging** (the logged
   token counts × AWS Price List rates) — it is **not** client-reported, so an untrusted
   client cannot inflate its own budget. The browser's live cost display is a
   non-authoritative UX estimate only. The broker refuses model credentials at the next
   short refresh once authoritative spend exceeds budget, and per-call `max_tokens` bounds
   each call — so the maximum overrun is a small, predictable amount of the user's *own*
   budget over the (log-delivery + credential-TTL) window, never cross-tenant.
   Institutions requiring exact pre-call enforcement enable **Tier 1**.

None of these is a cross-tenant or privilege-escalation risk. The isolation and
least-privilege guarantees hold under a hostile-client assumption.

---

## 11. What to take to the review

- The browser is **untrusted by design**; security rests on resource-side IAM/ABAC,
  not on client behavior.
- There is **no long-lived secret and no super-credential** anywhere in the system.
- **Tenant isolation is enforced below the application**, so app bugs cannot breach it.
- The **highest-priority review target** is the credential-vending broker (§10.1).
- Stricter postures (hard caps, centralized inspection) are available as **Tier 1**
  without changing the trust model — they add controls, they don't fix a flaw.

---

*Companion document: `agate-design.md` (full architecture and implementation plan).
The credential-vending broker and the ABAC tag scheme are specified there in §3.1 and
§13; they are the components this memo recommends for independent security review.*
