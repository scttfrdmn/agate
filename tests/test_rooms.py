"""Unit tests for collaborative scoped rooms — the security core (#116). No AWS.

The §7/§10 invariant: a room's reach is the INTERSECTION of its members' authorities —
adding a participant can only NARROW it, never widen it; a disjoint member is REFUSED, not
collapsed; every contribution is attributed and fenced to the room's scope.
"""

from __future__ import annotations

import pytest
from agate.rooms import (
    Member,
    RoomError,
    add_member,
    effective_member_tags,
    open_room,
    remove_member,
    room_cascade_nodes,
    room_message,
    room_scope,
    room_tier,
    room_to_saved_session,
)
from agate.session_record import Receipt, ReceiptRow, session_object_key
from agate.tags import SessionTags


def _tags(scope="chemistry", tier="frontier", tenant="uni", aff="researcher"):
    return SessionTags(
        affiliation=aff, tenant=tenant, courses=(), tier=tier, role="member", scope=scope
    )


def _member(subject, scope, *, kind="human", tier="frontier", tenant="uni"):
    return Member(kind=kind, subject=subject, tags=_tags(scope, tier=tier, tenant=tenant))


# --- N-way scope intersection ------------------------------------------------


def test_room_scope_picks_the_narrower_nested():
    assert room_scope(["chemistry", "chemistry/chem-101"]) == "chemistry/chem-101"


def test_room_scope_tenant_wide_only_when_all_unscoped():
    assert room_scope(["", ""]) == ""
    # an unscoped + a scoped member is the scoped one (narrower), NOT tenant-wide
    assert room_scope(["", "chemistry"]) == "chemistry"
    assert room_scope(["chemistry", ""]) == "chemistry"


def test_room_scope_rejects_disjoint_never_collapses():
    # The cardinal sin: two disjoint scoped members must RAISE, never silently become "".
    with pytest.raises(RoomError):
        room_scope(["chemistry", "physics"])


def test_room_tier_is_the_floor():
    assert room_tier(["frontier", "oss"]) == "oss"
    assert room_tier(["mid", "frontier"]) == "mid"
    assert room_tier([]) == "oss"


# --- the never-widen invariant (the headline) -------------------------------


def test_adding_a_member_only_narrows():
    r = open_room(_tags("chemistry"), room_id="lab", subject="prof")
    assert r.scope == "chemistry"
    r2 = add_member(r, _member("ta", "chemistry/chem-101"))
    assert r2.scope == "chemistry/chem-101"  # narrower
    # ⊆ both the prior room and the new member
    assert r2.scope.startswith("chemistry")


def test_disjoint_member_is_refused_room_unchanged():
    r = open_room(_tags("chemistry"), room_id="lab", subject="prof")
    with pytest.raises(RoomError):
        add_member(r, _member("phys", "physics"))
    # the original room object is immutable + unchanged
    assert r.scope == "chemistry"
    assert len(r.members) == 1


def test_cross_tenant_member_refused():
    r = open_room(_tags("chemistry", tenant="uni"), room_id="lab", subject="prof")
    with pytest.raises(RoomError):
        add_member(r, _member("spy", "chemistry", tenant="other"))


def test_tier_floor_across_members():
    r = open_room(_tags("chemistry/chem-101", tier="frontier"), room_id="lab", subject="prof")
    r = add_member(r, _member("stu", "chemistry/chem-101", tier="oss"))
    assert r.tier == "oss"


def test_remove_member_recomputes_exactly():
    r = open_room(_tags("chemistry"), room_id="lab", subject="prof")
    r = add_member(r, _member("ta", "chemistry/chem-101"))
    assert r.scope == "chemistry/chem-101"
    # removing the narrowing member widens back UP TO the remaining intersection (chemistry),
    # never beyond
    r = remove_member(r, "ta")
    assert r.scope == "chemistry"


# --- a member is clamped to the room ----------------------------------------


def test_effective_member_tags_clamps_a_broader_member():
    # A member broader than the room is bounded by the room (can't read beyond it).
    r = open_room(_tags("chemistry/chem-101"), room_id="lab", subject="prof")
    broad = _member("broad-agent", "chemistry", kind="agent", tier="frontier")
    # add a narrowing peer so the room is chem-101, then clamp the broad member
    r = add_member(r, broad)  # broad contains chem-101 -> room stays chem-101
    eff = effective_member_tags(r, broad)
    assert eff.scope == "chemistry/chem-101"  # clamped DOWN to the room
    assert eff.role == "member"


def test_effective_member_tags_clamps_tier_down():
    r = open_room(_tags("chemistry", tier="oss"), room_id="lab", subject="prof")
    big = _member("big", "chemistry", tier="frontier")
    r = add_member(r, big)  # room tier = oss
    eff = effective_member_tags(r, big)
    assert eff.tier == "oss"


# --- attributed message stream ----------------------------------------------


def test_message_is_attributed_to_the_verified_author():
    r = open_room(_tags("chemistry"), room_id="lab", subject="prof")
    msg = room_message(r, r.members[0], text="welcome", agent="uni/prof")
    assert msg.acting_as.on_behalf_of == "uni@prof"
    assert msg.acting_as.attributed is True
    assert msg.text == "welcome"


def test_agent_member_message_carries_its_agent_id():
    r = open_room(_tags("chemistry"), room_id="lab", subject="prof")
    r = add_member(r, _member("lit-agent", "chemistry", kind="agent"))
    agent_member = r.members[1]
    msg = room_message(r, agent_member, text="found 3 papers", agent="uni/lit-agent")
    assert msg.acting_as.agent == "uni/lit-agent"
    assert msg.kind == "agent"


def test_non_member_cannot_contribute():
    r = open_room(_tags("chemistry"), room_id="lab", subject="prof")
    intruder = _member("intruder", "chemistry")
    with pytest.raises(RoomError):
        room_message(r, intruder, text="i should not be here")


# --- transcript = a saved session (#109) ------------------------------------


def test_transcript_is_a_saved_session_under_room_scope():
    r = open_room(_tags("chemistry/chem-101"), room_id="lab-mtg", subject="prof")
    msg = room_message(r, r.members[0], text="hi", agent="uni/prof")
    receipt = Receipt(rows=(ReceiptRow("msg", "model", 0.02),), total=0.02)
    ss = room_to_saved_session(r, [msg], receipt, created="2026-06-16T00:00:00Z")
    assert ss.scope == "chemistry/chem-101"
    assert ss.tenant == "uni"
    # stored under the room's intersection scope -> fenced by the #80 policy like any session
    key = session_object_key(ss.tenant, ss.scope, ss.id)
    assert key == "uni/chemistry/chem-101/_sessions/lab-mtg.json"
    # the transcript entry carries the message's attribution
    assert ss.transcript[0]["actingAs"]["on_behalf_of"] == "uni@prof"


def test_transcript_receipt_self_validates():
    # a tampered total is rejected by Receipt.__post_init__
    with pytest.raises(Exception):  # noqa: B017 — SessionRecordError
        Receipt(rows=(ReceiptRow("a", "model", 0.01),), total=0.99)


# --- per-member budget cascade ----------------------------------------------


def test_room_cascade_has_one_node_per_member():
    r = open_room(_tags("chemistry"), room_id="lab", subject="prof")
    r = add_member(r, _member("ta", "chemistry/chem-101"))
    nodes = room_cascade_nodes(r, lambda m: (0.0, 10.0))
    labels = sorted(n[0] for n in nodes)
    assert labels == ["prof", "ta"]  # every member's budget is a ceiling
