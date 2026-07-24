"""Bounded delegation — a spawned agent's credential narrows the spawner's (#106, §2).

The other half of the keystone (with the #105 compiler): when a principal SPAWNS an
agent, the agent runs under a credential that is the **intersection** of the spawner's
verified authority and the agent spec — scope ∩ scope, tier = min, tenant held fixed,
courses inherited, role forced to member. A spawned/triggered/collaborating agent is
therefore NEVER more privileged than the principal it acts for (vision invariant §10.2),
and that is provable in STS — not asserted in a prompt.

The narrowing is pure (`delegate`, `scope_intersect`, `delegate_budget`) so it is fully
unit-testable and feeds the same proof-simulation the rest of the credential boundary
uses. The only AWS edge, `spawn_child`, takes its STS client as a parameter so even it
is testable with a fake (the broker/chokepoint assume pattern). Fail-closed throughout:
a scope conflict refuses to spawn rather than widening.

Transitivity is free: `delegate` maps `SessionTags -> SessionTags`, so a chain
`delegate(delegate(root, specA), specB)` only ever narrows — the basis for agent graphs
(#111).

Per-invoker instantiation (#107) builds straight on this: one authored agent shared to a
course `instantiate_for_invoker`s under EACH invoker's own verified tags, so invoker A and
invoker B get disjoint, own-scope-only credentials by construction — same agent, N
students, each confined to their own data, with no trusted roster list (eligibility is
read from the invoker's OWN verified courses/scope).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from agate.agentspec import AgentSpec, BudgetSpec
from agate.budget import _clean_id
from agate.entitlements import TIER_RANK, Tier
from agate.tags import ROLE_MEMBER, SessionTags, role_session_name


class DelegationError(ValueError):
    """A spawn that cannot be bounded safely (e.g. a scope conflict). Fail closed:
    refuse to spawn rather than vend a credential broader than the spawner's."""


def _contains(ancestor: str, node: str) -> bool:
    """True if scope `ancestor` contains `node` (ancestor-or-self), path-segment-wise.

    Mirrors `budget.is_within_admin_scope`'s containment: `chemistry` contains
    `chemistry/chem-101` but NOT `chem` and NOT `chemistry-annex` (no string-prefix
    bug). An empty `ancestor` is the whole tenant and contains everything."""
    ancestor, node = ancestor.strip("/"), node.strip("/")
    if not ancestor:
        return True
    return node == ancestor or node.startswith(ancestor + "/")


def scope_intersect(spawner_scope: str, spec_scope: str) -> str | None:
    """Intersect two scope paths by subtree containment.

    Returns the MORE SPECIFIC (deeper) scope when one contains the other, the shared
    value when equal, and **None when neither contains the other** (a conflict the
    caller must reject). An empty scope means "whole tenant" — it contains the other,
    so the intersection is the other. Never returns a scope broader than either input.
    """
    if _contains(spawner_scope, spec_scope):
        # spawner is an ancestor (or unscoped) -> child confined to the spec's deeper scope
        return spec_scope.strip("/")
    if _contains(spec_scope, spawner_scope):
        # spec is an ancestor (or unscoped) -> child confined to the spawner's deeper scope
        return spawner_scope.strip("/")
    return None  # disjoint subtrees — caller fails closed


def _min_tier(a: Tier, b: Tier) -> Tier:
    """The lower of two tiers by rank — a child can't exceed EITHER bound."""
    return a if TIER_RANK[a] <= TIER_RANK[b] else b


def delegate(spawner: SessionTags, spec: AgentSpec, *, subject: str = "") -> SessionTags:
    """The CHILD session tags for an agent spawned by `spawner` running `spec`.

    Every axis is the intersection of spawner ∩ spec, never a superset of either:
      * tenant  — the spawner's, verbatim (a spec never names a tenant; cross-tenant is
        structurally impossible).
      * tier    — min(spawner.tier, spec.tier) by TIER_RANK.
      * scope   — `scope_intersect`; a disjoint conflict raises DelegationError.
      * courses — inherited from the spawner (courses only narrow retrieval, never widen;
        the child's narrower scope bounds them further).
      * role    — always member: an agent is never an admin (admin gates the console, it
        is not a delegable capability). Fail-closed even if the spawner is admin.
      * affiliation — inherited (display/relevance only; tier is the real bound).

    `subject` is accepted for symmetry with the spawn path but does not affect the tags
    (the subject lives in the RoleSessionName, set by `spawn_child`).
    """
    child_scope = scope_intersect(spawner.scope, spec.scope)
    if child_scope is None:
        raise DelegationError(
            f"spec scope {spec.scope!r} is outside the spawner's scope {spawner.scope!r} "
            "(disjoint subtrees) — refusing to spawn"
        )
    return SessionTags(
        affiliation=spawner.affiliation,
        tenant=spawner.tenant,
        courses=spawner.courses,
        tier=_min_tier(spawner.tier, spec.tier),
        role=ROLE_MEMBER,
        scope=child_scope,
    )


def delegate_budget(
    spawner_remaining_usd: float | None, spec_budget: BudgetSpec | None
) -> float | None:
    """The child's spend ceiling: a slice of what the spawner has left, capped by the
    spec's own ask — `min(spec.budget.usd, spawner_remaining)`. None means unconstrained
    on that side; the result is None only when BOTH are None (no cap declared anywhere).

    Pure number logic. The real cascade row authorization (`budget.plan_budget_write`)
    runs at the live spawn against the spawner's actual remaining budget — this just
    computes the ceiling that write should use, so a child can never out-spend its parent.
    """
    spec_usd = spec_budget.usd if spec_budget is not None else None
    if spawner_remaining_usd is None and spec_usd is None:
        return None
    if spawner_remaining_usd is None:
        return spec_usd
    if spec_usd is None:
        return max(0.0, spawner_remaining_usd)
    return max(0.0, min(spec_usd, spawner_remaining_usd))


def spawn_child(
    child_tags: SessionTags,
    *,
    role_arn: str,
    subject: str,
    sts_client,
    duration_seconds: int = 900,
) -> dict:
    """Assume the agent role narrowed by the already-intersected `child_tags`, returning
    the STS credentials dict. The SAME verify→tags→assume pattern as the broker/chokepoint;
    the tags are the bounded child's, so the resulting session has EXACTLY the child's
    intersected authority — the spawn path cannot widen it.

    `role_session_name` encodes `<tenant>@<subject>` so attribution stays unforgeable down
    the chain (#79), and the tags are transitive so a further hop keeps the narrowing.
    `sts_client` is passed in (not a module global) so this is unit-testable with a fake.
    """
    tags = child_tags.to_sts_tags()
    resp = sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName=role_session_name(child_tags.tenant, subject),
        Tags=tags,
        TransitiveTagKeys=[t["Key"] for t in tags],
        DurationSeconds=duration_seconds,
    )
    return resp["Credentials"]


# --- per-invoker instantiation (#107) ---------------------------------------
# One authored agent, instantiated under each invoker's OWN verified credential. The
# isolation is structural: the child is delegate(invoker_tags, spec), so invoker A's
# child is bounded by A and invoker B's by B — disjoint by construction, no shared
# scope, no roster list to trust or forge.


def is_eligible_invoker(invoker: SessionTags, spec: AgentSpec) -> bool:
    """May this verified session instantiate `spec`? Answered from the invoker's OWN
    tags — never a live roster enumeration, never a client-supplied list.

    `spec.invokers` declares who may run the agent:
      * None        -> no restriction (eligible).
      * tenant      -> any session in the agent's tenant (always true: the broker only
                       vends same-tenant tags, so cross-tenant is already impossible).
      * roster:<c>  -> eligible iff course `<c>` is in the invoker's verified courses
                       (the LTI/broker-vended `agate:courses`). "On the roster" == the
                       invoker's OWN verified enrollment says so.
      * scope:<p>   -> eligible iff the invoker's scope and `<p>` OVERLAP (one contains
                       the other): a chair GOVERNING the subtree may run it, AND a
                       student WITHIN it may too (their instantiation narrows to their
                       own leaf). A sibling/disjoint scope cannot. This is exactly
                       `scope_intersect(...) is not None`.
    Anything else / unmatched -> False (fail closed)."""
    inv = spec.invokers
    if inv is None:
        return True
    if inv.kind == "tenant":
        return True  # same-tenant is structurally guaranteed; cross-tenant impossible
    if inv.kind == "roster":
        return inv.ref in invoker.courses
    if inv.kind == "scope":
        return scope_intersect(invoker.scope, inv.ref) is not None
    return False  # unknown kind — fail closed


def subject_key(subject: str) -> str:
    """An INJECTIVE, namespace-safe single segment for a subject id.

    `_clean_id` is LOSSY (it strips `/`,`|`,`:` etc., so distinct subjects like `a/b`
    and `ab` would clean to the same string and collide). Appending a 12-hex digest of
    the RAW subject makes the segment injective — different subjects get different keys
    regardless of how they clean. Load-bearing: a collision here would be a
    cross-principal memory/session leak (#107/#110). Shared by `invoker_namespace`
    (#107) and `agate.memory` (#110) so there is ONE definition."""
    digest = hashlib.sha256(subject.encode()).hexdigest()[:12]
    return f"{_clean_id(subject)}-{digest}"


def invoker_namespace(tenant: str, subject: str) -> str:
    """Stable, INJECTIVE per-invoker namespace key for memory/session isolation
    (#109/#110 consume it — two invokers must NEVER share one). `<clean_tenant>/<subject_key>`."""
    return f"{_clean_id(tenant)}/{subject_key(subject)}"


@dataclass(frozen=True, slots=True)
class InstantiatedAgent:
    """One authored agent, bound to one invoker. The per-invoker analogue of #105's
    `CompiledAgent`: `child_tags` (the invoker∩spec credential) feed
    `agentcompile.compile_agent` / `spawn_child`, and `namespace` isolates this
    invoker's memory/sessions from every other invoker of the same agent."""

    spec: AgentSpec
    invoker_subject: str
    child_tags: SessionTags
    namespace: str


def instantiate_for_invoker(
    invoker: SessionTags, spec: AgentSpec, *, subject: str
) -> InstantiatedAgent:
    """Instantiate `spec` for one verified invoker. Fail-closed: an ineligible invoker
    is refused (no credential), and the child credential is `delegate(invoker, spec)` —
    bounded by THIS invoker's verified authority ∩ the spec, never a classmate's.

    SECURITY: `invoker` MUST be a VERIFIED `SessionTags` (from `claims_to_tags` over a
    broker/LTI-validated token) — eligibility reads `invoker.courses`/`invoker.scope`,
    which must be unforgeable. Even if eligibility were too lax, the child is still
    `delegate(invoker, spec)`, so it stays bounded to the invoker's real scope (a student
    can't reach `physics` by claiming eligibility — `delegate` would raise on the disjoint
    scope). Eligibility is a gate on WHO may run; the credential narrowing is the boundary.

    Pure (no STS/DDB): returns the bound credential template + namespace. The live
    assume happens via `spawn_child` at the deferred instantiation endpoint."""
    if not is_eligible_invoker(invoker, spec):
        raise DelegationError(
            f"invoker is not eligible to run agent {spec.name!r} (invokers={spec.invokers})"
        )
    child = delegate(invoker, spec, subject=subject)
    return InstantiatedAgent(
        spec=spec,
        invoker_subject=subject,
        child_tags=child,
        namespace=invoker_namespace(child.tenant, subject),
    )
