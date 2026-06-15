# agate — The Agent Platform Vision

**From sessions to standing, governable agents.**
Status: forward-looking design. This is the *where-it-goes* document; the
authoritative as-built architecture remains [`agate-design.md`](agate-design.md).

> Each section marks **[built]** (exists today), **[seam]** (a current primitive that
> extends naturally), or **[vision]** (not yet built). The discipline of this doc is
> that nothing in it requires abandoning the foundation — every leap is a
> generalization of a primitive agate already trusts.

---

## 0. Thesis

A chatbot is a **session**: one human, one model, synchronous, stateless, no side
effects, forgotten when the tab closes. Every "AI gateway" ships that and calls it a
product. It is a commodity racing to zero.

agate's next iteration is a different **unit of work**:

> **A standing, named agent you specify once, delegate bounded authority to,
> collaborate with, and let act on your behalf — asynchronously and unattended —
> because its power is provably bounded in the credential, not hoped for in a prompt.**

The security substrate built so far (the claims→scoped-STS broker, ABAC session tags,
generated IAM, the budget cascade, IAM-enforced tenant/scope isolation) was never the
product — it is the **runway**. The reason no institution lets an agent act unattended
on FERPA data, submit HPC jobs, or touch the SIS is that they cannot *bound* it. agate
already makes authority unforgeable. That — and only that — is what licenses the jump
from "chatbot that answers" to "agent that acts."

**The wedge:** *the only AI platform where you can hand an agent real authority over
regulated, multi-tenant data and prove it cannot overreach.*

**And it does this without forking the ecosystem.** agate adopts the open agent stack
wholesale — MCP, Skills, A2A, AG-UI, A2UI, AP2, x402 (§8.6) — and contributes the one
layer those standards deliberately leave open: a **scoped credential under every
interaction**. The standards give interop; agate makes them safe in a regulated,
multi-tenant setting. Restated: *the open agent stack, governed.*

agate also doesn't have to build the agent *runtime* from scratch — see §0.1.

---

## 0.1 Relationship to agenkit — *agenkit builds the agent; agate governs it*

**[seam]** [agenkit](https://agenkit.dev) (Apache-2.0, same author) is "the foundation
layer for AI agents": a cross-language toolkit (Python / TypeScript / Go / Rust / C++ /
Zig at parity) giving the `Agent`/`Message`/`Tool` primitive, **orchestration patterns
(Sequential, Parallel, Router, Fallback, Conditional)**, resilience middleware (retry,
circuit breaker, timeout, rate-limit), pluggable transports (HTTP/gRPC/WebSocket), and
OpenTelemetry observability. By design it stops exactly where agate begins: it has **no
identity, authorization, multi-tenancy, budget, or cloud-binding** — those are "the
application's business." agate *is* that business. The two are complementary halves of
one stack, not competitors:

| Concern | agenkit | agate |
|---------|---------|-------|
| Agent primitive, orchestration patterns, resilience, transports, tracing, cross-language | ✅ owns it | consumes it |
| Identity, scoped credential, ABAC, tenant/scope isolation, budget cascade, AWS deployment | left to the app | ✅ owns it |

Three concrete consequences:

1. **agenkit's orchestration patterns *are* the agent graph (§4).** Sequential /
   Parallel / Router / Fallback / Conditional is the same vocabulary as Panel / Analyze
   / the router today (`agate.patterns`) — agenkit is the portable, battle-tested
   version. agate's move is to let an agenkit pattern *be* the spec's `reasoning`, and
   wrap each node in a narrowed credential (§2). The graph topology comes from agenkit;
   the authority boundary comes from agate.
2. **agate's AgentCore Runtime container runs agenkit agents.** The container
   (`agent/server.py`) becomes an agenkit host; the spec compiles to the scoped
   credential it runs under. Cross-language is a free win — agate agents stop being
   Python-locked (a Go/Rust agenkit agent for a hot path, still governed identically).
3. **The standards (§8.6) can land in *either* layer — by deliberate split.** agenkit
   is the author's project and can grow to speak MCP / A2A / AG-UI / A2UI / Skills /
   AP2 / x402 natively. The clean division of labor:
   - **agenkit owns the *protocol mechanics*** — speaking A2A on the wire, hosting MCP
     tools, emitting AG-UI/A2UI event streams, parsing an x402 402 response. Portable,
     reusable, identity-agnostic.
   - **agate owns the *authority under the protocol*** — every A2A peer call, MCP tool
     invocation, AG-UI stream, or x402 payment resolves to a narrowed credential +
     budget check. agenkit carries the message; agate decides whether it's allowed.

   So "does agenkit support AG-UI/AP2/etc. yet?" is the right question with the right
   answer: *not all of them today, but it's the natural home for the mechanics, and
   agate supplies the governance regardless of which layer the bytes flow through.*

**Tagline:** *agenkit builds the agent; agate governs it.*

---

## 1. The Agent Spec — an agent is a declarative artifact that compiles to a scoped identity

**[seam]** Today `agate.patterns.compile_pattern()` takes a declarative `Pattern`
(roster of roles + per-role model prefs + adjudicator) and compiles it *against the
caller's entitled models* into a dispatch payload. That is the seed. Generalize it:
an **agent is a spec that compiles to (a) a bounded credential, (b) an AgentCore
Runtime deployment, (c) budget rows, and (d) a trigger wiring** — the same way IAM
policies are generated from `entitlements`/`tags` today.

```yaml
# chem101-ta.agate.yaml
agent: chem101-ta
description: Drafts feedback on submitted problem sets for instructor review.
role: ta                       # derives tier + base entitlement (agate.entitlements)
scope: chemistry/chem-101      # the ONLY data subtree it may ever touch (agate:scope)
reasoning: panel               # a composable construct (agate.patterns), or an agent graph
tools:                         # MCP tools, via AgentCore Gateway — each scoped
  - course-materials-reader    # read-only, scope-confined
  - gradebook-drafts           # writes to a DRAFT queue, never the live gradebook
memory: per-invoker            # AgentCore Memory namespaced by the running identity
budget: $20 / student / term   # flows into the existing cascade (#81)
invokers: roster:chem-101      # LTI NRPS roster — who may run it
triggers:
  - on: lms:assignment-submitted
    then: draft-feedback
visibility: course             # who can see/instantiate it (a scope-tagged object)
```

**The load-bearing claim:** the spec *is* the agent's IAM. Its `scope`, `tools`,
`role`/tier, and `budget` compile into exactly the credential it runs under — the
agent **cannot exceed its spec because the spec became its policy**. This makes agents
**git-ops artifacts**: reviewable, versioned, diffable, signable, auditable. A PR that
widens an agent's `scope` shows up as an IAM diff in review. No consumer chatbot can
tell this governance story.

**The agent-spec compiler** (the first thing to build — see §9) is a direct
generalization of `policy.generate` + `agate.patterns`: `spec → {SessionTags template,
tool-scoped policy, Runtime config, budget rows, trigger bindings}`.

---

## 1.5 The interface is Claude-Code-like — multi-model by default, with *entitlement-aware* routing

**[seam]** The interaction model should feel like Claude Code, not a model picker: you
**state intent and the system routes** — multi-model by definition, with an "auto mode"
that chooses the right model and the right reasoning construct for the task. agate is
already multi-model (the tier→model map) and already has a router (`agate/router.py`
routes free-form input to Ask/Panel/Analyze, cheapest-default, user-override-wins). The
leap is to flip the default from *"pick a model"* to *"declare the task; auto-route
within what you're allowed and can afford."*

**The agate twist — routing is entitlement-and-budget-aware by construction.** Claude
Code routes on task difficulty; agate routes on difficulty **clamped to the session's
entitled model set and remaining budget**. Auto mode can *never* select a model the
tier doesn't permit or the budget can't afford, because **the router's candidate set IS
the entitled set** (`entitlements.models_for_tier`) and each candidate is pre-checked
against the cascade (#81). That is a routing story no commodity chat UI can tell: the
optimizer's search space is the credential's allow-set.

Two routing axes, one engine (generalizing the existing mode router):
- **which construct** — Ask / Panel / Analyze / (later) a named agent or graph;
- **which model** — the cheapest-that-clears-the-bar vs most-capable-affordable choice,
  *within the entitled set*.

**Routing policy is selectable; routing is transparent and overridable.** Ship named
policies — `thrifty` (cheapest entitled model that clears an estimated difficulty bar;
escalate only when warranted *and* affordable) and `best` (most capable the budget
comfortably affords; step down under pressure) — that a user picks per session or an
admin/`spec` pins per agent; auto picks a sensible default. Every turn **shows which
model handled it and why** (reusing the existing `route` + receipt/cost events), and the
user can **pin a specific entitled model anytime** — academics prefer explicit control,
and the cost/quality/entitlement tradeoff is a selling point, not something to hide. The
override-wins precedence is exactly `agate/router.py::resolve_mode` today, extended to
the model axis.

**Why this composes with the rest of the vision.** Auto mode at request time and the
agent spec's `reasoning`/model choice at compile time are the **same routing engine**,
resolved at two moments: a live session routes per turn; a compiled agent bakes its
policy into the spec. So the Claude-Code-like surface and the agent platform share one
governed router — and "auto" is safe everywhere because its candidate set is always the
bounded credential.

---

## 2. Bounded delegation — the unlock unique to agate

**[seam → vision]** When a principal spawns an agent, the agent's credential is a
**narrowing of the spawner's, never a superset.** This is AgentCore Identity converging
with the broker: the broker already vends `tenant@subject` session-named, tag-scoped
credentials; an agent run is an `AssumeRole` that *further* narrows (intersect the
spawner's scope with the spec's scope, take the lower tier, deduct from the spawner's
budget). Least privilege is **provable in STS**, not asserted in a system prompt.

The killer education move falls straight out of this:

> A professor authors **one** `chem101-ta` agent and shares it to the course. It
> **instantiates per-student under each student's own credential.** Same agent, 200
> students, each scoped to *their* submissions — the agent sees your work, never your
> classmate's.

Authored once, safely multiplied by the roster. The commercial options would need an
app-layer multi-tenancy check you'd have to *trust*; agate makes the cross-student read
**unrepresentable in the credential** the agent holds.

---

## 3. Saved sessions & cross-session memory

**[built today]** A session is synchronous and ephemeral — closing the tab forgets it.

**[vision] Saved sessions.** A session becomes a **first-class, scope-tagged, persisted
object**: transcript + the receipts (every model call, cost, citation, tool
invocation, and the *scope under which it ran*) + referenced artifacts in scoped
storage. Because it carries its scope tag, a saved session is shareable *within the
boundary that produced it* — you can hand a colleague in the same course/lab a session
and they can resume it only if their own credential authorizes that scope. Resume,
fork, branch ("what if I'd asked it this instead"), and replay are all natural. A saved
session is also the honest audit record: "prove what this agent did and under whose
authority" is answerable because the receipts were authoritative server-side, never
client-claimed (the same discipline as spend metering, #79).

**[vision] Cross-session memory — three tiers, each identity-scoped:**

| Tier | Scope | Backed by | Example |
|------|-------|-----------|---------|
| **Session memory** | one conversation | the saved session | the current thread |
| **Personal memory** | one principal, across sessions | AgentCore Memory, namespaced by `subject` | "my research agent remembers my project over weeks" |
| **Shared memory** | a scope node | AgentCore Memory, namespaced by `agate:scope` | a lab's collective context, readable by every lab member, *never* across labs |

The non-negotiable invariant: **memory is namespaced by the same ABAC tags as
everything else.** Personal memory can never leak across principals; shared memory can
never leak across tenants/scopes. Memory becomes just another resource the credential
fences — so "the agent remembers" stops being a privacy hazard and becomes a governed
feature. This is the thing chatbots get *dangerously* wrong (one global memory blob)
and agate gets right *by construction*.

---

## 4. Agents working with other agents (controlled)

**[seam]** Panel/Analyze already orchestrate multiple model calls with a roster and an
adjudicator. Generalize the roster into an **agent graph**: a node may be a model *or
another agent*, each independently scoped.

```
grant-writer (scope: lab/photonics)
 ├─▶ literature-agent     (read-only, scope: lab/photonics + tenant-wide pubs)
 ├─▶ budget-agent         (read: lab allocation; no write)
 └─▶ compliance-agent     (read: institutional policy corpus)
        → adjudicator composes → draft for human review
```

**The control model — three hard rules so "agents calling agents" can't become a
privilege-escalation or runaway-cost engine:**

1. **Monotonic narrowing.** A sub-agent's credential is the *intersection* of its
   spec and its caller's authority. A child can never out-scope its parent. (Same
   `AssumeRole`-narrowing as §2, applied transitively. TransitiveTagKeys already carry
   the tags down a chain — built for exactly this.)
2. **Budget flows down and is debited up.** A child draws from the parent's remaining
   budget via the existing cascade (#81); the parent's ceiling is the family ceiling.
   Depth and fan-out caps are spec fields. No infinite recursion, no surprise bill.
3. **Every hop is attributed and metered.** The call graph is the audit graph — who
   invoked whom, under what scope, at what cost — because session-name attribution
   (#79) already makes each hop unforgeable in the logs.

This is where *"let users define their own reasoning constructs"* actually lands:
composable, **governed** agent graphs, not a free-for-all.

---

## 5. Tools & connectors — turning the campus into governed capabilities

**[seam → vision]** AgentCore Gateway exposes MCP tools to the Runtime; the agent's
*outbound* identity is its scoped execution role. Two distinct things hang off this, and
agate governs them at **different layers** — conflating them mis-applies the wrong fence:

- A **tool is a VERB** — a callable action the agent invokes mid-reasoning (submit an HPC
  job, draft feedback, query an API). The **action plane**, governed by the spec's tool
  grant (#105 `tool_policy`) + bounded delegation: a tool call inherits the caller's
  narrowed credential and can only act *within* its scope.
- A **connector is a NOUN** — a standing integration to a content system (Google Drive,
  Box, MS Teams, Discord, NFS, S3) whose *content flows into agate*, primarily as
  ingestion into the scoped corpus / vector index. The **data plane**, governed by the
  #80/#84 `{tenant}/{scope}` fence (connector content lands under a scoped prefix/index,
  fenced like every document). A connector answers *"what can the agent ground in"*; a
  tool answers *"what can it do."*

Both ride AgentCore Gateway's mechanics (targets: OpenAPI, Smithy, Lambda; 1-click
Salesforce/Slack/Jira/etc.; outbound OAuth user-delegated|autonomous + API keys), and the
agate contribution is identical to §8.6: Gateway gives the *connection*; agate supplies the
*authority* under it. For SaaS content systems, **user-delegated OAuth** is the key mode —
the agent acts AS the verified user, so the source's own ACL composes with agate's scope
(defense in depth: both must allow it). S3 is direct via the scoped role (#80/#84, no
Gateway); NFS isn't a managed target (wrap in a Lambda tool, or a Fargate sidecar — it
doesn't fit the serverless microVM). NOTE on compute: AgentCore Runtime is a serverless
microVM host with **no instance-type/GPU knob** (managed; good for NO CLOCKS) — a workload
needing specific hardware would diverge onto Fargate/EC2/Batch.

**Campus tools/connectors, each fronted by the same credential discipline:**

- **Library catalog / discovery** → read-only search *tool*.
- **The LMS** (LTI 1.3 + NRPS, already integrated) → roster, assignments, a
  *draft-only* feedback channel that requires human promotion to go live.
- **The SIS / registrar** → read-scoped to the caller's own records.
- **Research data** → S3-backed datasets, scope-confined (the #84 vector boundary
  already generalizes here).
- **The HPC scheduler** → *directly relevant to the sibling Gauss/Slurm work*: turn
  Slurm into an MCP tool so a researcher's agent can **submit, monitor, and summarize
  jobs against their own allocation** — bounded by their scope and budget. This is a
  concrete, non-chatbot capability that no commodity gateway offers.

- **Content systems** (Google Drive, Box, MS Teams, Discord) → *connectors*: their
  content ingests into the tenant/scope corpus + vector index (fenced by #80/#84), via
  Gateway's OpenAPI target + user-delegated OAuth (the agent reads only what the user can).

**The rule for every tool AND connector:** it inherits the caller's bounded credential.
A tool cannot widen reach (acts only within the agent's scope); a connector's content
lands only under the agent's `{tenant}/{scope}` (read only via the fence). "Connect
anything" is safe precisely because connection ≠ authority — authority is still the
credential. Third-party MCP servers / SaaS connectors run under a spec-declared, scoped
egress identity (user-delegated where possible), never the user's raw credential.

---

## 6. Triggered & durable — the actual difference from a chatbot

**[vision]** A chatbot only runs when you type. An agent earns its name by working
unattended:

- **Scheduled** (EventBridge): "every Monday, summarize new arXiv papers in my field
  and file them to my project."
- **Event-driven** (S3 / LMS / EventBridge): a dataset lands → the lab agent profiles
  it; an assignment is submitted → the TA agent drafts feedback for instructor review.
- **Durable multi-step** (Step Functions): long-running research or grant workflows
  that survive across hours and checkpoints.

Triggers are spec fields, and a triggered run still assumes the *spawner's* bounded
credential at fire time — an unattended agent is no more privileged than the human who
authored it. NO CLOCKS holds: triggers are per-event Lambda/Step Functions invocations,
nothing idles.

---

## 7. Collaboration — humans **and** agents in scoped rooms

**[vision]** A **workspace is itself a scope-tagged object**; humans and agents are
both participants, each carrying their own bounded reach.

> A lab meeting: three researchers + a literature-review agent + a data-analysis agent
> in a shared, persistent, real-time room. Every contribution — human or agent — is
> attributed and metered; the agents can touch only what the room's collective scope
> authorizes; the transcript is a saved session (§3).

Real-time fan-out via AppSync/WebSocket; artifacts in scoped storage; the room's scope
is the *intersection* of its members' authorities (so adding an agent can never widen
what the room can reach). Agent-to-agent collaboration (§4) and human-agent
collaboration are the **same primitive** — participants with credentials in a
scope-bounded space.

---

## 8. Why this is a category, not a feature

The chatbot is a commodity. A **governable agent platform where authority *is*
identity, and agents are declarative, delegatable, collaborative, durable artifacts**
is a category — and agate has already built the one part everyone else hand-waves: the
unforgeable boundary. Universities are the beachhead because FERPA + the LTI roster +
per-capita entitlement make scoping *concrete and mandatory*. But the identical thesis
sells to any regulated, multi-tenant organization: research consortia, hospitals,
agencies. "Hand an agent real authority and prove it can't overreach" is the universal
need behind every stalled enterprise-agent pilot.

---

## 8.5 Authoring surfaces — graphical, beginner-first, **without weakening the model**

**[vision]** The agent spec (§1) and its permissions are YAML today, which suits
experts. The progression is to make authoring **graphical and beginner-first** — and
the load-bearing insight is that *this does not soften the security model, it can be
the safest surface of all.*

**Why a GUI is safe — even safer — here.** Every authoring path, no matter how simple,
funnels through the **same agent-spec compiler** (§1), which emits provable IAM, and
obeys the same invariants (§10). A beginner clicking buttons is *exactly as bounded* as
an expert writing YAML, because the boundary was never in the authoring surface — it is
in the compiler plus the narrowing rule (§2). The GUI is a front-end to a compiler that
cannot emit an over-broad credential.

Three reasons the graphical surface is genuinely **better**, not a dumbed-down version:

1. **The permission model already has a graphical shape.** Scope is a tree (school →
   dept → course/lab) → a tree picker. Tier is ordered → a labeled choice ("standard /
   advanced / frontier"). Budget is a number → a dollar field with a live remaining-cap
   readout. Tools are a checklist of plain-language *capabilities* ("read course
   materials", "draft feedback — instructor approves before it goes live"). You never
   show IAM JSON; you show **intent**.
2. **Unsafe choices are unrepresentable, not rejected.** Because delegation only
   narrows (§2), the scope picker can only render nodes the author actually holds — a
   student literally *cannot click* `physics/phys-202` because it isn't in their tree.
   "You can't escalate" stops being a validation error and becomes **the absence of the
   button**. The safest UX is the one where the bad path does not exist.
3. **agate can render the *effective* boundary, not just the stated one.** The classic
   IAM tragedy is that nobody knows what a policy actually grants. Because agate
   *generates* the credential, it can show, in plain language, "here is everything this
   agent can touch / do / spend" — computed from the compiled spec, the same way the
   `iam:SimulateCustomPolicy` proofs work today, surfaced for humans.

**The authoring ladder (all rungs round-trip to one spec):**

| Rung | Surface | Who | How bounded |
|------|---------|-----|-------------|
| **Template gallery** | pick "TA feedback agent", fill 2 blanks | absolute beginner | institution pre-bounded the template |
| **Visual builder** | tree-scope + capability checklist + "when X → do Y" rules | most authors | pickers only offer what the author holds |
| **Natural language** | "summarize new papers in my lab every Monday" | anyone | LLM *drafts* the spec; compiler clamps + confirms |
| **YAML / graph editor** | the raw spec + agent-graph (§4) | experts | the spec itself |

**The natural-language rung is the ultimate beginner surface — and stays safe by the
same principle.** An LLM turns a sentence into a *draft* spec; the compiler renders the
bounded plan ("this agent will read X, may draft Y, can spend ≤ $Z, runs Mondays") for
human confirmation. Even a hallucinated over-broad scope is **clamped to what the author
actually holds** before anything compiles. *The LLM proposes; the compiler disposes* —
authority never originates from the model's suggestion, only from the author's real
entitlement. This is the one place an LLM touches the permission path, and it touches it
only as an untrusted *drafter*, never as the source of authority.

---

## 8.6 Standing on open standards — agate as the *governance layer* for the open agent stack

**[vision]** agate must not invent a proprietary agent stack. The industry is
converging on open protocols for exactly the pieces the spec (§1) currently hand-waves
— *how agents talk, how capabilities package, how agents render UI, how agents pay.*
agate's job is not to replace them but to **put a scoped-credential boundary under each
one.** Standards give interoperability; agate gives the bounded authority that makes
them safe in a regulated, multi-tenant setting. That is the whole differentiator
restated: *the open agent stack, governed.* Per the §0.1 split, the **protocol
mechanics** naturally live in agenkit (portable, identity-agnostic) and the **authority
under them** in agate — so "agate speaks X" below means "agate governs X, via whichever
layer carries the bytes," not that every protocol is implemented today.

Each standard answers a question the spec leaves open, and each gets the same
treatment — **the credential, not the protocol, is the authority:**

| Standard | What it is | What it answers for agate | agate's governing move |
|----------|-----------|---------------------------|------------------------|
| **MCP** | tool/resource connectivity | "what can an agent *touch*" (§5) | every tool call inherits the caller's bounded credential; third-party servers run under a spec-declared scoped egress identity |
| **Skills** | portable, model-agnostic capability packages (instructions + resources) | "what an agent *knows how to do*" — the reusable unit behind the spec's `reasoning`/capability slot | a Skill runs **under the agent's bounded credential**; *which* skills an agent may load is a spec field, hence IAM-governed. `agate.patterns` is already a proto-skill registry — generalize it to load/compose Skills |
| **A2A** (Agent-to-Agent) | wire protocol for agents invoking agents | "how the §4 agent graph nodes actually talk" | the agent card advertises capability, but **authority is the narrowed assumed-role, never the card's claims**; every A2A hop is monotonic-narrowing + metered (§4) |
| **AG-UI** | streaming agent state/events to a UI | "how a session/room renders live + interoperably" (§3, §7) | replaces agate's bespoke event protocol; the event stream is still scope-tagged and attributed, so a live UI shows only what the credential authorized |
| **A2UI** | agent-generated *interactive* UI — live panels, not just text | "the next iteration of the chatbot" — Panel/Analyze output becomes a **live dashboard**, not a transcript | rendered components are bounded by the same scope; an agent can only surface data/actions its credential permits, so a "live panel" can't become an exfiltration or privileged-action surface |
| **AP2** (Agent Payments Protocol) | mandate/intent-based authorization for agent *purchases* | "how an agent is permitted to *spend money* on the user's behalf" | the budget cascade (#81) **is** the spending mandate — a spec's `budget` becomes the signed, scoped authorization an AP2 mandate carries; an agent can't transact beyond its compiled ceiling |
| **x402** | HTTP 402 revival — per-request payments over the wire (pay-per-call APIs/tools/data) | "how an agent pays for a metered tool/data call inline" | a 402-priced call is just another metered action: it's pre-checked against remaining budget (the chokepoint pattern), debited up the cascade, and attributed per-hop — no agent runs up an unbounded bill |

**Two principles hold across all of them:**

1. **Adopt the standard for interop; own the boundary for safety.** agate speaks MCP /
   A2A / AG-UI / A2UI so an agate agent interoperates with the wider ecosystem (and
   external agents/tools can participate *within* a scope) — but every cross-boundary
   interaction is mediated by a narrowed credential. A protocol message is a *request*;
   authority is always the assumed role. This is the §10 invariant applied to wire
   formats: a capability advertised over A2A, a tool offered over MCP, or a panel
   action emitted over A2UI is inert until it resolves to scoped IAM.

2. **Standards are spec fields, so they're governed by authoring (§8.5).** "May this
   agent load this Skill / call this A2A peer / connect this MCP server / render this
   panel action" are all checkboxes in the visual builder, all compiled to IAM, all
   clamped to what the author holds. The open stack inherits agate's "unsafe is
   unrepresentable" property for free.

The flagship payoff is **A2UI live panels** for the "beyond just another chatbot" goal:
a research session that streams a *live, interactive* panel — a dataset profile that
updates as a job runs, a budget gauge, a citation graph you can click — instead of a
wall of text. Built on the standard, governed by the credential, metered per the
cascade.

**Payments (AP2 / x402) are where agate's existing work pays off unusually well.** The
unsolved problem in agent payments is *bounded autonomy*: how do you let an agent spend
without it spending too much, on the wrong thing, or on someone else's dime? agate
already answers this for model tokens — authoritative server-side metering (#79),
pre-call budget checks (the Tier-1 chokepoint), and a hierarchical budget cascade
(#81). Generalize "spend" from tokens to *any* priced action and:

- a spec's `budget` becomes a **signed spending mandate** (the thing AP2 wants),
  scoped to exactly that agent and debited up the school → dept → lab cascade;
- an **x402-priced tool/data call is pre-authorized against remaining budget** before
  it fires (the chokepoint pattern, unchanged) and attributed per-hop in the agent
  graph (§4), so a runaway sub-agent can't drain the family ceiling;
- delegation still only narrows (§2): a child agent's spending authority is a subset of
  its parent's, so "my research agent may buy datasets up to $50/mo" cannot become "and
  so can every sub-agent it spawns, each up to $50."

This turns agate from "governs what an agent can *read and run*" into "governs what an
agent can *read, run, and pay for*" — with the same single boundary. For a regulated,
multi-tenant institution, *bounded agent spend* is at least as load-bearing as bounded
data access, and agate is already most of the way there.

---

## 9. Build order (each step rests on the prior; each is a generalization, not a rewrite)

> **Tracked in GitHub** as three phases: **Phase 10** (foundation — spec compiler +
> bounded delegation, tracking #101), **Phase 11** (capabilities — memory, sessions,
> graphs, tools, triggers, tracking #102), **Phase 12** (surfaces & ecosystem — rooms,
> authoring, standards, payments, tracking #103). The numbered steps below map onto
> those milestones in order.

1. **Agent-spec compiler** *(the keystone — everything hangs off it).* `spec →
   {SessionTags template, tool-scoped IAM, Runtime config, budget rows, triggers}`.
   Direct generalization of `policy.generate` + `agate.patterns.compile_pattern`.
   Pure + unit-testable + proof-simulated, like every load-bearing part before it.
2. **Bounded delegation** (§2): the spawn-narrows-credential `AssumeRole` path + the
   per-invoker instantiation. Proof-sim: a child can never out-scope its parent.
2.5. **Entitlement-aware routing / auto mode** (§1.5): extend `agate/router.py` to a
   second axis (which model), candidate set = the entitled set, each pre-checked against
   the budget cascade; selectable `thrifty`/`best` policy; transparent + override-wins.
   Foundational because the agent spec's `reasoning`/model choice and live auto mode are
   the same engine.
3. **Saved sessions + personal/shared memory** (§3), ABAC-namespaced.
4. **Agent graphs** (§4) with monotonic narrowing + cascade budget + attribution.
5. **MCP tool catalog** (§5), starting with read-only LMS/library, then the HPC
   scheduler as the flagship "agent that acts."
6. **Triggers** (§6), then **collaborative rooms** (§7).
7. **Open-standard adoption** (§8.6), threaded through steps 4–6 rather than bolted on:
   MCP for tools, Skills as the capability unit, A2A as the agent-graph wire, AG-UI /
   A2UI for live panels, AP2 / x402 for agent payments. Each is adopted for interop and
   wrapped in a narrowed credential — the protocol is the request, the credential is the
   authority.
8. **Authoring surfaces** (§8.5), layered on once the compiler is solid: template
   gallery → visual builder → natural-language drafting → graph editor. Built *last*
   on purpose — they are front-ends to a compiler that must already be unbreakable, so
   the GUI can only ever express what the compiler already bounds. The "effective
   boundary" view reuses the `iam:SimulateCustomPolicy` proof machinery, surfaced for
   humans.

Each step ships behind the same gates as the foundation: plan-mode for anything
security-critical, generated IAM over inline, a live `iam:SimulateCustomPolicy` proof
for every new boundary, and a pre-merge security review on credential/data paths.

---

## 10. The invariants this vision must never violate

These are the load-bearing constraints — if a feature can't honor them, it doesn't
ship:

1. **Authority is the credential.** No capability is enforced only in app code or a
   prompt; everything reduces to scoped IAM the way docs (#80) and vectors (#84) do.
   This is *why* a graphical or natural-language authoring surface (§8.5) is safe: the
   boundary lives in the compiler, not the UI, so an LLM or a beginner can only ever
   *propose* — authority still originates from the author's real entitlement.
2. **Delegation only narrows.** A spawned/triggered/collaborating agent is never more
   privileged than the principal it acts for.
3. **Memory and sessions are ABAC-namespaced.** Persistence is just another fenced
   resource; it never becomes a cross-tenant/cross-principal leak.
4. **Everything is attributed and metered, server-side — including money.** The call
   graph is the audit graph; budgets cascade; nothing is client-claimed (#79). When an
   agent *pays* (AP2 / x402, §8.6), the budget cascade is the spending mandate: every
   priced action is pre-authorized against remaining budget and debited up the tree, so
   bounded spend has the same guarantee as bounded access.
5. **Open standards in, narrowed credential under.** agate speaks the open agent stack
   (MCP, Skills, A2A, AG-UI, A2UI, AP2, x402) for interop, but a protocol message is
   only ever a *request* — authority resolves to scoped IAM (§8.6). agate adds the
   governance layer the standards deliberately leave open; it never forks them.
6. **NO CLOCKS.** Standing agents are still per-event; "always available" must not mean
   "always billing."
