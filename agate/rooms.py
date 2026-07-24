"""Collaborative scoped rooms — the security core (#116, vision §7).

A **room is a scope-tagged object** in which humans AND agents are participants, each carrying
its own bounded credential. The load-bearing invariant (§10): the room's reach is the
**INTERSECTION** of its members' authorities — adding a participant can only NARROW it, never
widen it. Human-agent and agent-agent collaboration are the SAME primitive: bounded
participants in a scope-bounded space (Panel was the single-user prototype — N model voices,
one human; a room is N humans + N agents).

This module is PURE and AWS-free. It composes the existing boundary primitives rather than
inventing new ones:
  * `delegate.scope_intersect`/`_min_tier` (#106) — pairwise narrowing, folded N-way here.
  * `identity.acting_as_from_session` (#137) — every message is attributed (who · on whose
    authority), recovered from the verified RoleSessionName, never a client field.
  * `session_record` (#109) — a room transcript IS a SavedSession, fenced by the #80 policy.

The genuinely-new logic is the N-way intersection with a fail-closed twist: `scope_intersect`
returns `""` (tenant-wide) for an unscoped input, which is correct for delegation but a TRAP
for a room — two DISJOINT scoped members must be REJECTED, never silently collapsed to `""`
(which would WIDEN the room to the whole tenant, the cardinal sin). So a room's scope is `""`
only when EVERY member is unscoped; any disjoint pair raises `RoomError`.

The live transport (AppSync/WebSocket fan-out, per-message budget debit) and the collaboration
UX (turn-taking, panes, presence) are deferred follow-ups; this is the provable core.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
from typing import Literal

from agate.delegate import _min_tier, scope_intersect
from agate.entitlements import Tier
from agate.identity import ActingAs, acting_as_from_session
from agate.session_record import Receipt, SavedSession, build_saved_session
from agate.tags import ROLE_MEMBER, SessionTags, role_session_name

MemberKind = Literal["human", "agent"]


class RoomError(ValueError):
    """A room that cannot be formed safely — a disjoint member (no common scope subtree),
    a cross-tenant member, or a non-member trying to contribute. Fail closed: refuse rather
    than widen the room's reach or admit an unattributed contribution."""


# --- N-way authority intersection -------------------------------------------


def room_scope(scopes: list[str]) -> str:
    """The room's scope = the narrowest common subtree of its members' scopes.

    Folds `delegate.scope_intersect` (which returns the deeper of two nested scopes, or
    None when neither contains the other). A None at any step means two members are in
    DISJOINT subtrees → `RoomError` (fail-closed). The result is `""` (tenant-wide) ONLY
    when every member is unscoped — never as a side effect of collapsing a disjoint pair,
    which would WIDEN the room (the invariant this guards). Empty input → `""`.
    """

    def step(acc: str, nxt: str) -> str:
        inter = scope_intersect(acc, nxt)
        if inter is None:
            raise RoomError(
                f"member scope {nxt!r} is disjoint from the room's scope {acc!r} "
                "(no common subtree) — refusing to admit (would not narrow)"
            )
        return inter

    if not scopes:
        return ""
    return reduce(step, scopes)


def room_tier(tiers: list[Tier]) -> Tier:
    """The room's tier = the LEAST-privileged tier across members (`_min_tier` folded).
    A room with a frontier researcher and an oss student is oss — no member acts above the
    room's collective floor. Empty input → oss (the lowest)."""
    if not tiers:
        return "oss"
    return reduce(_min_tier, tiers)


# --- the room + member model ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Member:
    """A room participant. `tags` is the member's OWN verified-or-delegated credential
    (an agent member's tags come from `delegate(...)`, so they are already ⊆ its author;
    the room then intersects again). `subject` is the verified subject id (human) or the
    stable agent id (agent) — the attribution key for this member's contributions."""

    kind: MemberKind
    subject: str
    tags: SessionTags


@dataclass(frozen=True, slots=True)
class Room:
    """A scope-tagged collaboration space. `scope`/`tier` are DERIVED (the intersection of
    `members`) and recomputed on every membership change, so the never-widen invariant
    cannot drift. All members share one `tenant` (cross-tenant is structurally impossible)."""

    id: str
    tenant: str
    members: tuple[Member, ...]
    scope: str
    tier: Tier

    def has_member(self, subject: str) -> bool:
        return any(m.subject == subject for m in self.members)


def _derive(tenant: str, members: tuple[Member, ...]) -> tuple[str, Tier]:
    """Compute the room's (scope, tier) from its members. Asserts the single-tenant
    invariant and folds the N-way intersection (fail-closed on a disjoint member)."""
    for m in members:
        if m.tags.tenant != tenant:
            raise RoomError(
                f"member {m.subject!r} tenant {m.tags.tenant!r} != room tenant {tenant!r} "
                "(cross-tenant rooms are impossible)"
            )
    scope = room_scope([m.tags.scope for m in members])
    tier = room_tier([m.tags.tier for m in members])
    return scope, tier


def open_room(creator: SessionTags, *, room_id: str, subject: str) -> Room:
    """Open a room from its creator (its first member). The room's scope/tier start as the
    creator's; every later member can only narrow them."""
    member = Member(kind="human", subject=subject, tags=creator)
    scope, tier = _derive(creator.tenant, (member,))
    return Room(id=room_id, tenant=creator.tenant, members=(member,), scope=scope, tier=tier)


def add_member(room: Room, member: Member) -> Room:
    """Return a NEW room with `member` added and the scope/tier re-derived. Fail-closed: a
    disjoint member raises `RoomError` and the room is unchanged. The returned scope is
    provably ⊆ the prior room's scope AND ⊆ the new member's scope (intersection only
    narrows) — adding a participant can never widen the room's reach (§10)."""
    members = (*room.members, member)
    scope, tier = _derive(room.tenant, members)  # raises on disjoint / cross-tenant
    return Room(id=room.id, tenant=room.tenant, members=members, scope=scope, tier=tier)


def remove_member(room: Room, subject: str) -> Room:
    """Return a NEW room without the named member, scope/tier re-derived from scratch (so a
    removal that lifts a constraint widens the room back to — but never beyond — the
    remaining members' intersection). Removing the last member leaves an empty,
    tenant-wide-by-vacuity room; callers close such rooms."""
    members = tuple(m for m in room.members if m.subject != subject)
    scope, tier = _derive(room.tenant, members)
    return Room(id=room.id, tenant=room.tenant, members=members, scope=scope, tier=tier)


def effective_member_tags(room: Room, member: Member) -> SessionTags:
    """The credential a member ACTS with INSIDE the room: its own tags clamped to the
    room's collective reach (scope ∩ room.scope, tier = min). So even a member broader than
    the room is bounded by the room — "an agent added to a room cannot read beyond the
    room's scope". Reuses the same `scope_intersect`/`_min_tier` narrowing as delegation."""
    clamped_scope = scope_intersect(member.tags.scope, room.scope)
    if clamped_scope is None:
        # A current member can't be disjoint from the room it's in (add_member guarantees
        # it), but stay fail-closed: clamp to the room scope rather than widen.
        clamped_scope = room.scope
    return SessionTags(
        affiliation=member.tags.affiliation,
        tenant=member.tags.tenant,
        courses=member.tags.courses,
        tier=_min_tier(member.tags.tier, room.tier),
        role=ROLE_MEMBER,
        scope=clamped_scope,
    )


# --- attributed message stream ----------------------------------------------


@dataclass(frozen=True, slots=True)
class RoomMessage:
    """One attributed contribution to a room. `acting_as` (#137) carries who · on whose
    authority · within what remit — recovered from the verified session, never client-set,
    so a room transcript is an unforgeable audit of who said what under whose authority."""

    author_subject: str
    kind: MemberKind
    text: str
    acting_as: ActingAs

    def to_dict(self) -> dict:
        return {
            "author": self.author_subject,
            "kind": self.kind,
            "text": self.text,
            "actingAs": self.acting_as.to_dict(),
        }


def room_message(room: Room, member: Member, *, text: str, agent: str = "") -> RoomMessage:
    """Build an attributed message from `member`. Fail-closed: a non-member cannot
    contribute (`RoomError`). The author's OBO record is recovered from the VERIFIED
    RoleSessionName (`<tenant>@<subject>`), never a client field — so attribution can't be
    forged. `agent` is the acting agent id for an agent author (defaults to the member's
    subject); a human author acts as itself within the room's remit."""
    if not room.has_member(member.subject):
        raise RoomError(
            f"{member.subject!r} is not a member of room {room.id!r} — cannot contribute"
        )
    eff = effective_member_tags(room, member)
    session_name = role_session_name(room.tenant, member.subject)
    acting = acting_as_from_session(
        session_name,
        agent=agent or member.subject,
        remit={"scope": eff.scope, "tier": eff.tier, "room": room.id},
    )
    return RoomMessage(author_subject=member.subject, kind=member.kind, text=text, acting_as=acting)


# --- transcript = a saved session (#109) ------------------------------------


def room_to_saved_session(
    room: Room,
    messages: list[RoomMessage],
    receipt: Receipt,
    *,
    created: str,
    mode: str = "room",
) -> SavedSession:
    """A room transcript IS a `SavedSession` (#109): stored under the room's intersection
    scope (`{tenant}/{room_scope}/_sessions/`), so the #80 data-scope policy + #84 retrieval
    filter fence it exactly like any session. Each transcript entry carries its message's
    `ActingAs`, so the saved record is an attributed audit. `receipt` self-validates (total
    == sum of rows). `created` is stamped by the caller (NO CLOCKS here)."""
    return build_saved_session(
        session_id=room.id,
        tenant=room.tenant,
        scope=room.scope,
        subject=room.id,  # the room itself is the provenance owner of its transcript
        created=created,
        mode=mode,
        transcript=[m.to_dict() for m in messages],
        receipt=receipt,
    )


def room_cascade_nodes(room: Room, spend_lookup) -> list[tuple[str, float, float | None]]:
    """The `cost.evaluate_cascade` node-list for an action in the room: one `(label, spend,
    budget)` row PER MEMBER, so an action must fit under EVERY member's remaining budget —
    the flat, peer analogue of the agent-graph's ancestor cascade (#112). `spend_lookup(
    Member) -> (spend, budget|None)` is injected (the transport supplies live numbers; tests
    fake it), keeping this pure. The live debit is the transport's job (deferred, like
    #115/#136)."""
    rows: list[tuple[str, float, float | None]] = []
    for m in room.members:
        spend, budget = spend_lookup(m)
        rows.append((m.subject, spend, budget))
    return rows
