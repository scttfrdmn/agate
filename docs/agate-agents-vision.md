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

## 5. MCP connectivity — turning the campus into governed tools

**[seam → vision]** AgentCore Gateway exposes MCP tools to the Runtime; the agent's
*outbound* identity is its scoped execution role. The vision is to make **campus
systems first-class MCP tools, each fronted by the same credential discipline:**

- **Library catalog / discovery** → read-only search tool.
- **The LMS** (LTI 1.3 + NRPS, already integrated) → roster, assignments, a
  *draft-only* feedback channel that requires human promotion to go live.
- **The SIS / registrar** → read-scoped to the caller's own records.
- **Research data** → S3-backed datasets, scope-confined (the #84 vector boundary
  already generalizes here).
- **The HPC scheduler** → *directly relevant to the sibling Gauss/Slurm work*: turn
  Slurm into an MCP tool so a researcher's agent can **submit, monitor, and summarize
  jobs against their own allocation** — bounded by their scope and budget. This is a
  concrete, non-chatbot capability that no commodity gateway offers.

**The rule for every tool:** a tool call inherits the caller's bounded credential. A
tool cannot widen reach; it can only act *within* the scope the agent already holds.
"Connect any tool" is safe precisely because connection ≠ authority — authority is
still the credential. Third-party MCP servers (a vendor's tool) run under a
spec-declared, scoped egress identity, never the user's raw credential.

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

## 9. Build order (each step rests on the prior; each is a generalization, not a rewrite)

1. **Agent-spec compiler** *(the keystone — everything hangs off it).* `spec →
   {SessionTags template, tool-scoped IAM, Runtime config, budget rows, triggers}`.
   Direct generalization of `policy.generate` + `agate.patterns.compile_pattern`.
   Pure + unit-testable + proof-simulated, like every load-bearing part before it.
2. **Bounded delegation** (§2): the spawn-narrows-credential `AssumeRole` path + the
   per-invoker instantiation. Proof-sim: a child can never out-scope its parent.
3. **Saved sessions + personal/shared memory** (§3), ABAC-namespaced.
4. **Agent graphs** (§4) with monotonic narrowing + cascade budget + attribution.
5. **MCP tool catalog** (§5), starting with read-only LMS/library, then the HPC
   scheduler as the flagship "agent that acts."
6. **Triggers** (§6), then **collaborative rooms** (§7).

Each step ships behind the same gates as the foundation: plan-mode for anything
security-critical, generated IAM over inline, a live `iam:SimulateCustomPolicy` proof
for every new boundary, and a pre-merge security review on credential/data paths.

---

## 10. The invariants this vision must never violate

These are the load-bearing constraints — if a feature can't honor them, it doesn't
ship:

1. **Authority is the credential.** No capability is enforced only in app code or a
   prompt; everything reduces to scoped IAM the way docs (#80) and vectors (#84) do.
2. **Delegation only narrows.** A spawned/triggered/collaborating agent is never more
   privileged than the principal it acts for.
3. **Memory and sessions are ABAC-namespaced.** Persistence is just another fenced
   resource; it never becomes a cross-tenant/cross-principal leak.
4. **Everything is attributed and metered, server-side.** The call graph is the audit
   graph; budgets cascade; nothing is client-claimed (#79).
5. **NO CLOCKS.** Standing agents are still per-event; "always available" must not mean
   "always billing."
