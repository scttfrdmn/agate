"""Unit tests for room records (#116). No AWS — pure.

A room is a scope-tagged S3 object whose KEY prefix (`{tenant}/{scope}/`) is the #80 fence
(mirrors SavedSession/SavedAgent). `from_json` rehydrates members; scope/tier are re-derived by
the handler, not trusted from the stored values. The key sanitises every segment so a crafted
scope/id can't escape the tenant/scope/_rooms prefix.
"""

from __future__ import annotations

import json

import pytest
from agate.room_record import (
    RoomRecordError,
    build_saved_room,
    from_json,
    room_object_key,
)
from agate.rooms import Member, open_room
from agate.tags import SessionTags


def _tags(*, tenant="uni", scope="chemistry/chem-101", tier="frontier"):
    return SessionTags(
        affiliation="researcher", tenant=tenant, courses=("chem-101",), tier=tier, scope=scope
    )


def _room():
    room = open_room(_tags(), room_id="lab-1", subject="prof")
    return room


# --- key derivation: tenant-rooted (room = tenant metadata, scope re-derived) ---


def test_key_is_tenant_rooted():
    # A room object is keyed at the tenant root (not scope-keyed): its scope narrows as members
    # join, and a narrower joiner must still locate it. Tenant isolation is the IAM fence.
    assert room_object_key("uni", "lab-1") == "uni/_rooms/lab-1.json"


def test_key_sanitises_crafted_id_no_path_injection():
    key = room_object_key("uni", "../../evil/r")
    assert key.startswith("uni/_rooms/")
    tail = key.split("/_rooms/")[1]
    assert "/" not in tail  # no `/` injection escaped the prefix


def test_key_requires_tenant_and_id():
    with pytest.raises(RoomRecordError):
        room_object_key("", "r")
    with pytest.raises(RoomRecordError):
        room_object_key("uni", "///")


# --- round-trip ------------------------------------------------------------


def test_roundtrip_preserves_members_and_messages():
    room = _room()
    msgs = [{"author": "prof", "kind": "human", "text": "hi", "actingAs": {}}]
    saved = build_saved_room(room, msgs)
    back = from_json(saved.to_json())
    assert back.room.id == "lab-1"
    assert back.room.tenant == "uni"
    assert [m.subject for m in back.room.members] == ["prof"]
    assert back.room.members[0].tags.scope == "chemistry/chem-101"
    assert back.messages == tuple(msgs)


def test_roundtrip_preserves_agent_member_kind():
    room = open_room(_tags(), room_id="r", subject="prof")
    from agate.rooms import add_member

    agent = Member(kind="agent", subject="uni/paper-sweep", tags=_tags())
    room = add_member(room, agent)
    back = from_json(build_saved_room(room, []).to_json())
    kinds = {m.subject: m.kind for m in back.room.members}
    assert kinds["uni/paper-sweep"] == "agent"
    assert kinds["prof"] == "human"


def test_from_json_rejects_missing_members():
    with pytest.raises(RoomRecordError):
        from_json(json.dumps({"id": "r", "tenant": "uni"}))


def test_from_json_rejects_bad_member_kind():
    bad = {
        "id": "r",
        "tenant": "uni",
        "scope": "",
        "tier": "oss",
        "members": [{"kind": "alien", "subject": "x", "tags": {"tenant": "uni"}}],
        "messages": [],
    }
    with pytest.raises(RoomRecordError):
        from_json(json.dumps(bad))
