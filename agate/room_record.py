"""Saved rooms — a room is a scope-tagged S3 object (#116 live transport).

A collaborative room (#116, vision §7) persists as a scope-tagged object at
`{tenant}/{room_scope}/_rooms/{room_id}.json`, so its access control IS the
`{tenant}/{room_scope}/` prefix fence — the same boundary as documents (#80), sessions (#109),
and created agents (#118). The object holds the room's members (each member's kind/subject +
their own verified-or-delegated `SessionTags`), the server-DERIVED scope/tier, and the
append-only message log (each entry the `RoomMessage.to_dict()` shape — text + the #137
`ActingAs` attribution).

This module is PURE (no boto3): it (de)serialises the room envelope + derives the scope-confined
key. The rooms Lambda re-derives scope/tier from the members on every load via `agate.rooms`
(never trusting a stored scope), assumes a tenant-fenced role to read/write the object, and is
the AWS edge. The write fence (`policy.generate.room_rw_policy`) confines a writer to its OWN
`{tenant}/{scope}/_rooms/*` subtree — so a member can only persist a room under a scope it holds
(it cannot forge a room broader than itself). Last-write-wins on the object is acceptable for a
lab-meeting cadence (a conditional-write hardening is a follow-up).

The member tags are persisted because re-deriving the room's scope on a mutation needs EVERY
member's authority, and only the acting member's token is present in a given request. The acting
member's own entry is refreshed from its verified token on each mutation (the handler's job); the
stored tags of OTHER members are trusted only up to the writer's own write-fence scope.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agate.budget import _clean_id
from agate.rooms import Member, Room
from agate.tags import ROLE_MEMBER, SessionTags

_ROOMS_SEGMENT = "_rooms"


class RoomRecordError(ValueError):
    """A room record that cannot be built/parsed safely (bad scope/id/shape)."""


def _tags_to_dict(t: SessionTags) -> dict:
    return {
        "affiliation": t.affiliation,
        "tenant": t.tenant,
        "courses": list(t.courses),
        "tier": t.tier,
        "role": t.role,
        "scope": t.scope,
    }


def _tags_from_dict(d: dict) -> SessionTags:
    return SessionTags(
        affiliation=str(d.get("affiliation", "student")),  # type: ignore[arg-type]
        tenant=str(d["tenant"]),
        courses=tuple(str(c) for c in d.get("courses", ())),
        tier=str(d.get("tier", "oss")),  # type: ignore[arg-type]
        role=str(d.get("role", ROLE_MEMBER)),
        scope=str(d.get("scope", "")),
    )


def _member_to_dict(m: Member) -> dict:
    return {"kind": m.kind, "subject": m.subject, "tags": _tags_to_dict(m.tags)}


def _member_from_dict(d: dict) -> Member:
    kind = d.get("kind")
    if kind not in ("human", "agent"):
        raise RoomRecordError(f"invalid member kind: {kind!r}")
    return Member(kind=kind, subject=str(d["subject"]), tags=_tags_from_dict(d["tags"]))


@dataclass(frozen=True, slots=True)
class SavedRoom:
    """A persisted room: its members + derived scope/tier + the append-only message log.
    `messages` are `RoomMessage.to_dict()` dicts (text + ActingAs). The cursor is the log
    length — a monotonic integer the polling read uses (`events?since=N`)."""

    room: Room
    messages: tuple[dict, ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.room.id,
                "tenant": self.room.tenant,
                "scope": self.room.scope,
                "tier": self.room.tier,
                "members": [_member_to_dict(m) for m in self.room.members],
                "messages": list(self.messages),
            },
            indent=2,
            sort_keys=True,
        )


def build_saved_room(room: Room, messages: list[dict] | None = None) -> SavedRoom:
    """Assemble a SavedRoom from a `Room` (whose scope/tier the caller already re-derived via
    `agate.rooms`) + the message log."""
    return SavedRoom(room=room, messages=tuple(messages or ()))


def from_json(text: str) -> SavedRoom:
    """Parse a saved-room object. The members are rehydrated; the caller (the rooms handler)
    RE-DERIVES scope/tier from them via `agate.rooms` rather than trusting the stored values —
    so a tampered stored scope can't widen the room (the never-widen invariant holds at load)."""
    d = json.loads(text)
    members = d.get("members")
    if not isinstance(members, list):
        raise RoomRecordError("saved room has no members list")
    room = Room(
        id=str(d["id"]),
        tenant=str(d["tenant"]),
        members=tuple(_member_from_dict(m) for m in members),
        scope=str(d.get("scope", "")),
        tier=str(d.get("tier", "oss")),  # type: ignore[arg-type]
    )
    msgs = d.get("messages", [])
    if not isinstance(msgs, list):
        raise RoomRecordError("saved room messages must be a list")
    return SavedRoom(room=room, messages=tuple(msgs))


def room_object_key(tenant: str, room_id: str) -> str:
    """The S3 key for a room: `{tenant}/_rooms/{id}.json` — TENANT-rooted, NOT scope-keyed.

    A room object is coordination METADATA (members + message log), not scoped data: its scope
    is the INTERSECTION of members, which NARROWS as members join, so a scope-keyed object would
    have to move and a narrower joiner (confined to a child subtree) couldn't even read a room
    at a parent scope. The real fences are elsewhere: `rooms.effective_member_tags` clamps what
    a member may DO inside the room, the transcript is a SavedSession written under the
    intersection scope (#80), and MEMBERSHIP is checked in the handler (it's dynamic, not
    IAM-expressible). So the object is tenant-fenced (`room_rw_policy`) + handler-membership-gated;
    cross-tenant is structurally impossible (the `{tenant}/` prefix). The id is sanitised so a
    crafted value can't inject `/` levels escaping the `{tenant}/_rooms/` prefix."""
    t = _clean_id(tenant)
    if not t:
        raise RoomRecordError("tenant is required for a room key")
    rid = _clean_id(room_id)
    if not rid:
        raise RoomRecordError("room_id did not normalise to a valid id")
    return f"{t}/{_ROOMS_SEGMENT}/{rid}.json"
