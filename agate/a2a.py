"""A2A — external-peer admission, governed (#119 slice, vision §8.6 / §4 / §0.1).

The headline open-standard interop contribution and the agenkit/agate split made concrete:
an external agent's **card** advertises capability, but **authority is the narrowed
assumed-role, never the card's claims.** An external A2A peer admitted to agate's agent graph
is bounded EXACTLY like an in-tenant sub-agent (#111/#112) — its authority is
`delegate(caller, request)`: scope ∩ caller, tier = min(caller, requested), tenant fixed to
the caller's, a disjoint request rejected (#106). The card is a *request*; the credential is
the *authority*.

Per §0.1: **agenkit owns the wire** (peer discovery, card exchange, the on-the-wire message
format, the live remote assume-role that provisions the credential); **agate owns the
authority under it.** This module is the PURE governance core — it takes the caller's verified
`SessionTags` + the untrusted peer request and returns the bounded credential + an external-
marked attribution record. No STS, no transport, no AWS.

The headline property, provable purely: the admitted authority depends ONLY on
`delegate(caller, request)`. Whatever else the card advertises (a higher tier, a broader
scope, extra capabilities) is inert — `admit_peer` reads only the requested scope + role and
clamps both to the caller. A peer participating in agate's graph is bounded by an assumed
role, not its card.
"""

from __future__ import annotations

from dataclasses import dataclass

from agate.agentspec import role_to_tier
from agate.delegate import _min_tier, scope_intersect
from agate.entitlements import Tier
from agate.identity import ActingAs, acting_as_from_session, agent_id
from agate.tags import ROLE_MEMBER, SessionTags, role_session_name

# The marker that brands an external peer's agent id: `{tenant}/external-{name}`. A HYPHEN
# (not a colon) so it survives `identity._clean_id` (grammar `[a-zA-Z0-9._-]` — `:` is
# stripped); the audit then plainly shows "this hop was an external A2A peer".
_EXTERNAL_PREFIX = "external-"


class A2AError(ValueError):
    """An external peer that cannot be admitted safely — a requested scope disjoint from the
    caller's (the card trying to reach outside the caller's subtree). Fail closed: refuse
    rather than admit a peer broader than the caller."""


@dataclass(frozen=True, slots=True)
class PeerRequest:
    """The UNTRUSTED ask derived from an A2A agent card. EVERY field here is a CLAIM — none
    of it grants anything. `admit_peer` reads only `requested_scope` + `requested_role` and
    clamps both to the caller; `name`/`origin` are recorded as provenance, never trusted for
    authority. The agenkit wire fills this from a received card."""

    name: str
    requested_scope: str = ""
    requested_role: str = "researcher"  # → tier via role_to_tier (a CLAIM, clamped to caller)
    origin: str = ""  # where the card came from (hostname/URL) — provenance only


@dataclass(frozen=True, slots=True)
class AdmittedPeer:
    """An external peer admitted under the caller's authority. `child_tags` is the bounded
    credential the agenkit wire provisions (the live remote assume-role); `acting_as` (#137)
    is the external-marked attribution (the peer acts on the caller's authority, recorded as
    external with the card's origin as untrusted provenance)."""

    peer_request: PeerRequest
    child_tags: SessionTags
    acting_as: ActingAs


def admit_peer(
    caller_tags: SessionTags, peer_request: PeerRequest, *, subject: str
) -> AdmittedPeer:
    """Admit an external A2A peer under the caller's VERIFIED authority. The clamp IS the
    boundary: the peer's credential = caller ∩ request (scope `scope_intersect`, tier
    `min`, tenant fixed, role forced member) — IDENTICAL to the #106 narrowing every
    sub-agent gets, so the card's advertised tier/scope are irrelevant. A requested scope
    disjoint from the caller's raises `A2AError` (fail-closed — the card can't reach out of
    the caller's subtree).

    The peer's authority depends ONLY on `(caller_tags, requested_scope, requested_role)` —
    no other card field can widen it. The hop is attributed as external (agent id
    `{tenant}/external-{name}`) on the verified ROOT user's authority, with the card's origin
    recorded as untrusted provenance. Pure: no STS — the wire (agenkit) provisions the live
    credential from `child_tags`."""
    child_scope = scope_intersect(caller_tags.scope, peer_request.requested_scope)
    if child_scope is None:
        raise A2AError(
            f"peer requested scope {peer_request.requested_scope!r} is outside the caller's "
            f"scope {caller_tags.scope!r} (disjoint subtrees) — refusing to admit"
        )
    requested_tier: Tier = role_to_tier(peer_request.requested_role)
    child_tags = SessionTags(
        affiliation=caller_tags.affiliation,
        tenant=caller_tags.tenant,
        courses=caller_tags.courses,
        tier=_min_tier(caller_tags.tier, requested_tier),
        role=ROLE_MEMBER,  # an external peer is never an admin (same as any delegated agent)
        scope=child_scope,
    )
    acting = acting_as_from_session(
        role_session_name(child_tags.tenant, subject),
        agent=agent_id(child_tags.tenant, f"{_EXTERNAL_PREFIX}{peer_request.name}"),
        remit={
            "tier": child_tags.tier,
            "scope": child_tags.scope,
            "external": True,
            "origin": peer_request.origin,  # untrusted provenance — not an authority
        },
    )
    return AdmittedPeer(peer_request=peer_request, child_tags=child_tags, acting_as=acting)


def peer_cascade_nodes(
    caller_path: tuple[str, ...], admitted: AdmittedPeer, spend_lookup
) -> list[tuple[str, float, float | None]]:
    """The `cost.evaluate_cascade` node-list for an external peer's call: one (label, spend,
    budget) row per CALLER ancestor + the peer node, so the peer's spend must fit under EVERY
    ancestor's budget — the family ceiling (#112), so a runaway external peer can't drain the
    caller's budget. `spend_lookup(label) -> (spend, budget|None)` is injected (the executor
    supplies live numbers; tests fake it), keeping this pure. Mirrors `graph.cascade_nodes`."""
    labels = [*caller_path, admitted.acting_as.agent]
    rows: list[tuple[str, float, float | None]] = []
    for label in labels:
        spend, budget = spend_lookup(label)
        rows.append((label, spend, budget))
    return rows
