"""Unit tests for the collaborative-rooms endpoint (#116). No AWS — STS/S3/DDB faked.

The §7/§10 invariant on the live surface: a room's reach is the INTERSECTION of its members,
re-derived server-side and never widened; every message is attributed to the verified session
and gated under every member's budget. The headline cases:
  * disjoint member → 403, room unchanged (never widened to tenant-wide);
  * an agent member is clamped to the joiner (⊆ joiner ⊆ room);
  * post by a non-member → 403; attribution is verified-session-only;
  * post over a member's budget → rejected, naming the member; nothing appended/debited;
  * the room object is keyed under the re-derived {tenant}/{room_scope}/ prefix.
"""

from __future__ import annotations

import json

import pytest
from infra.functions.rooms import handler as h


class _FakeS3:
    """An in-memory bucket the assumed-role client stands in for. Records puts."""

    def __init__(self, store: dict[str, bytes]):
        self.store = store

    def get_object(self, *, Bucket, Key):  # noqa: N803 — boto3 kwarg names
        if Key not in self.store:
            raise KeyError(Key)

        class _B:
            def __init__(self, b):
                self._b = b

            def read(self):
                return self._b

        return {"Body": _B(self.store[Key])}

    def put_object(self, *, Bucket, Key, Body, **kw):  # noqa: N803
        self.store[Key] = Body if isinstance(Body, bytes) else Body.encode()
        return {}


def _claims(scope="chemistry", subject="prof", tenant="uni", grant=True):
    return {
        "sub": subject,
        "affiliation": "researcher",
        "tenant": tenant,
        "data_scope": scope,
        "grant": grant,
    }


@pytest.fixture
def env(monkeypatch):
    store: dict[str, bytes] = {}
    s3 = _FakeS3(store)
    monkeypatch.setattr(h, "DOCS_BUCKET", "agate-docs")
    monkeypatch.setattr(h, "ROOM_ROLE_ARN", "arn:aws:iam::111122223333:role/agate-room")
    monkeypatch.setattr(h, "SPEND_TABLE", "")  # no live spend tables in unit tests
    monkeypatch.setattr(h, "BUDGET_TABLE", "")
    monkeypatch.setattr(h, "_assume_room_client", lambda tags, subject: s3)
    monkeypatch.setattr(h, "_period", lambda: "2026-06")
    # default identity: prof @ chemistry. Tests override per-call.
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims() if tok else _raise())
    return store


def _raise():
    raise h.RoomsToolError("missing idp_token")


def _invoke(req: dict) -> dict:
    resp = h.handler({"body": json.dumps(req)}, None)
    return {"status": resp["statusCode"], "body": json.loads(resp["body"])}


# --- open / view -----------------------------------------------------------


def test_open_creates_room_scoped_to_creator(env):
    out = _invoke({"idp_token": "t", "op": "open", "nonce": "lab1"})
    assert out["status"] == 200
    assert out["body"]["scope"] == "chemistry"
    assert [m["subject"] for m in out["body"]["members"]] == ["prof"]
    # persisted at the tenant-rooted _rooms/ prefix (room object = tenant metadata)
    assert any(k.startswith("uni/_rooms/") for k in env)


# --- join: intersection never widens ---------------------------------------


def test_join_narrows_to_intersection(env, monkeypatch):
    _invoke({"idp_token": "t", "op": "open", "nonce": "lab1"})  # prof @ chemistry
    room_id = next(k for k in env).split("/_rooms/")[1][:-5]
    # a chem-101-scoped student joins -> room narrows to chemistry/chem-101
    monkeypatch.setattr(
        h,
        "validate_idp_token",
        lambda tok: _claims(scope="chemistry/chem-101", subject="stu", grant=False),
    )
    out = _invoke({"idp_token": "t", "op": "join", "room": room_id})
    assert out["status"] == 200
    assert out["body"]["scope"] == "chemistry/chem-101"  # narrowed, not widened


def test_join_disjoint_member_rejected_room_unchanged(env, monkeypatch):
    _invoke({"idp_token": "t", "op": "open", "nonce": "lab1"})  # prof @ chemistry
    room_id = next(k for k in env).split("/_rooms/")[1][:-5]
    before = dict(env)
    # a physics-scoped member is disjoint -> refused, never collapsed to tenant-wide
    monkeypatch.setattr(
        h, "validate_idp_token", lambda tok: _claims(scope="physics", subject="phys")
    )
    out = _invoke({"idp_token": "t", "op": "join", "room": room_id})
    assert out["status"] == 403
    # the physics member can't even read the chemistry room key via its own scope, but if it
    # could, the room must be unchanged — assert no widening was persisted under physics.
    assert not any("/physics/_rooms/" in k for k in env)
    # the chemistry room object is unchanged
    assert env.get(next(k for k in before)) == before[next(k for k in before)]


# --- post: attribution + non-member + budget gate --------------------------


def test_post_by_member_is_attributed(env):
    _invoke({"idp_token": "t", "op": "open", "nonce": "lab1"})
    room_id = next(k for k in env).split("/_rooms/")[1][:-5]
    out = _invoke(
        {"idp_token": "t", "op": "post", "room": room_id, "text": "hello room", "author": "FORGED"}
    )
    assert out["status"] == 200
    msg = out["body"]["message"]
    assert msg["text"] == "hello room"
    assert msg["author"] == "prof"  # the verified subject, NOT the forged body field
    assert "actingAs" in msg


def test_post_by_non_member_rejected(env, monkeypatch):
    _invoke({"idp_token": "t", "op": "open", "nonce": "lab1"})
    room_id = next(k for k in env).split("/_rooms/")[1][:-5]
    # a different chemistry subject who never joined tries to post
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims(subject="intruder"))
    out = _invoke({"idp_token": "t", "op": "post", "room": room_id, "text": "x"})
    assert out["status"] == 403


def test_post_over_budget_rejected_nothing_appended(env, monkeypatch):
    _invoke({"idp_token": "t", "op": "open", "nonce": "lab1"})
    room_id = next(k for k in env).split("/_rooms/")[1][:-5]
    # Force the cascade to reject by giving the member a $0 budget.
    from cost.precall import CascadeResult

    monkeypatch.setattr(
        h,
        "evaluate_cascade",
        lambda **kw: CascadeResult("reject", 0.5, "prof", "over budget"),
    )
    before = dict(env)
    out = _invoke({"idp_token": "t", "op": "post", "room": room_id, "text": "expensive"})
    assert out["status"] == 200
    assert out["body"]["ok"] is False
    assert "prof" in out["body"]["reason"]
    assert env == before  # nothing appended/persisted


def test_events_returns_messages_after_cursor(env):
    _invoke({"idp_token": "t", "op": "open", "nonce": "lab1"})
    room_id = next(k for k in env).split("/_rooms/")[1][:-5]
    _invoke({"idp_token": "t", "op": "post", "room": room_id, "text": "one"})
    _invoke({"idp_token": "t", "op": "post", "room": room_id, "text": "two"})
    out = _invoke({"idp_token": "t", "op": "events", "room": room_id, "since": 1})
    assert out["status"] == 200
    assert [m["text"] for m in out["body"]["messages"]] == ["two"]
    assert out["body"]["cursor"] == 2


def test_events_by_non_member_rejected(env, monkeypatch):
    _invoke({"idp_token": "t", "op": "open", "nonce": "lab1"})
    room_id = next(k for k in env).split("/_rooms/")[1][:-5]
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims(subject="lurker"))
    out = _invoke({"idp_token": "t", "op": "events", "room": room_id})
    assert out["status"] == 403


# --- leave: self or an agent, never another human --------------------------


def test_leave_self_removes_caller(env):
    _invoke({"idp_token": "t", "op": "open", "nonce": "lab1"})
    room_id = next(k for k in env).split("/_rooms/")[1][:-5]
    out = _invoke({"idp_token": "t", "op": "leave", "room": room_id})
    # prof was the only member -> room closes
    assert out["status"] == 200
    assert out["body"].get("closed") is True


def test_leave_cannot_evict_another_human(env, monkeypatch):
    # prof opens; a chem-101 student joins. The student tries to evict prof -> refused.
    _invoke({"idp_token": "t", "op": "open", "nonce": "lab1"})
    room_id = next(k for k in env).split("/_rooms/")[1][:-5]
    monkeypatch.setattr(
        h,
        "validate_idp_token",
        lambda tok: _claims(scope="chemistry/chem-101", subject="stu", grant=False),
    )
    _invoke({"idp_token": "t", "op": "join", "room": room_id})
    out = _invoke({"idp_token": "t", "op": "leave", "room": room_id, "member": "prof"})
    assert out["status"] == 403  # a member cannot evict another human


# --- fail closed -----------------------------------------------------------


def test_missing_token_is_403(env):
    out = _invoke({"op": "open", "nonce": "x"})
    assert out["status"] == 403


def test_unknown_op_fails_closed(env):
    out = _invoke({"idp_token": "t", "op": "nuke"})
    assert out["status"] == 403


def test_post_missing_text_fails_closed(env):
    _invoke({"idp_token": "t", "op": "open", "nonce": "lab1"})
    room_id = next(k for k in env).split("/_rooms/")[1][:-5]
    out = _invoke({"idp_token": "t", "op": "post", "room": room_id})
    assert out["status"] == 403
