"""Unit tests for the AgentCore Memory read/write server (#130). No AWS.

The load-bearing assertions: the handler derives EVERY namespace/actor from the verified
identity via `agate.memory.namespaces_for` — never from the client — and fails closed. A
client-supplied `namespace`/`actorId`/`tenant`/`scope` in the body is ignored; a tier the
session doesn't have (shared-when-unscoped) is rejected; a bad/missing token is rejected.
"""

from __future__ import annotations

import json

import pytest
from agate.delegate import subject_key

# The handler builds its boto3 client at import time; patch it per-test.
from infra.functions.memory import handler as h


class _FakeAgentCore:
    """Records the kwargs of every SDK call so tests can assert what was sent."""

    def __init__(self):
        self.create_calls = []
        self.retrieve_calls = []

    def create_event(self, **kw):
        self.create_calls.append(kw)
        return {"event": {"eventId": "evt-1"}}

    def retrieve_memory_records(self, **kw):
        self.retrieve_calls.append(kw)
        return {"memoryRecordSummaries": [{"content": "remembered"}]}


_CLAIMS = {
    "sub": "alice",
    "tenant": "chem",
    "affiliation": "student",
    "courses": ["chem-101"],
    # data_scope (#80) is the data-access confinement claim — distinct from admin "scope".
    "data_scope": "chemistry/chem-101",
}
_UNSCOPED_CLAIMS = {k: v for k, v in _CLAIMS.items() if k != "data_scope"}


@pytest.fixture
def fake(monkeypatch):
    fc = _FakeAgentCore()
    # The handler assumes a tag-scoped role per request and builds the AgentCore client from
    # the returned creds; patch that seam to hand back the fake (and record the tags it would
    # have passed, so a test can assert the tenant/scope fence travels on the assumed session).
    fc.assume_tags = []

    def _fake_assume(tags, subject):
        fc.assume_tags.append((tags.tenant, tags.scope, subject))
        return fc

    monkeypatch.setattr(h, "assume_memory_client", _fake_assume)
    monkeypatch.setattr(h, "MEMORY_ID", "agate_memory-xyz")
    monkeypatch.setattr(h, "MEMORY_ACCESS_ROLE_ARN", "arn:aws:iam::111122223333:role/agate-mem")
    # Bypass real JWKS verification: the verified claims are the test's input.
    monkeypatch.setattr(h, "validate_idp_token", lambda token: dict(_CLAIMS) if token else _raise())
    return fc


def _raise():
    raise h.MemoryToolError("missing idp_token")


def _invoke(req: dict) -> dict:
    """Call the Lambda handler and return the parsed body."""
    resp = h.handler({"body": json.dumps(req)}, None)
    return {"status": resp["statusCode"], "body": json.loads(resp["body"])}


# --- record: server-derived actor/session, never the client -----------------


def test_record_uses_server_derived_actor_and_session(fake):
    out = _invoke(
        {
            "idp_token": "t",
            "op": "record",
            "session_id": "s-1",
            "payload": [{"role": "user", "text": "hi"}],
        }
    )
    assert out["status"] == 200
    call = fake.create_calls[0]
    # actorId is tenant-qualified (unambiguous `@` separator) + injective subject_key —
    # NOT a client value.
    assert call["actorId"] == f"chem@{subject_key('alice')}"
    assert call["sessionId"] == "s-1"
    assert call["memoryId"] == "agate_memory-xyz"


def test_record_ignores_client_supplied_namespace_and_identity(fake):
    # A malicious body naming another tenant/namespace/actor must have NO effect.
    _invoke(
        {
            "idp_token": "t",
            "op": "record",
            "session_id": "s-1",
            "payload": [{"role": "user", "text": "hi"}],
            "namespace": "agate/evil/personal/x/",
            "actorId": "evil",
            "tenant": "evil",
            "scope": "evil/scope",
        }
    )
    call = fake.create_calls[0]
    assert "evil" not in call["actorId"]
    assert call["actorId"].startswith("chem@")
    assert "namespace" not in call  # create_event has no client namespace param


def test_record_assumes_role_with_verified_tenant_and_scope(fake):
    # The IAM fence depends on the assumed session carrying the verified agate: tags — the
    # handler must pass the tenant/scope from the token, not the body, to assume_memory_client.
    _invoke(
        {
            "idp_token": "t",
            "op": "record",
            "session_id": "s-1",
            "payload": [{"role": "user", "text": "hi"}],
            "tenant": "evil",
            "scope": "evil/scope",
        }
    )
    tenant, scope, subject = fake.assume_tags[0]
    assert tenant == "chem"  # from the verified token, NOT the body's "evil"
    assert scope == "chemistry/chem-101"
    assert subject == "alice"


def test_record_requires_payload(fake):
    out = _invoke({"idp_token": "t", "op": "record", "session_id": "s-1"})
    assert out["status"] == 403
    assert fake.create_calls == []


def test_record_requires_session_id(fake):
    out = _invoke({"idp_token": "t", "op": "record", "payload": [{"role": "user", "text": "hi"}]})
    assert out["status"] == 403


# --- recall: namespacePath from namespaces_for, never the client ------------


def test_recall_personal_uses_derived_namespace(fake):
    out = _invoke({"idp_token": "t", "op": "recall", "tier": "personal"})
    assert out["status"] == 200
    ns = fake.retrieve_calls[0]["namespace"]
    assert ns == f"agate/chem/personal/{subject_key('alice')}/"
    assert out["body"]["namespace"] == ns


def test_recall_ignores_client_namespace(fake):
    _invoke(
        {
            "idp_token": "t",
            "op": "recall",
            "tier": "personal",
            "namespace": "agate/evil/personal/x/",
            "namespacePath": "agate/evil/",
        }
    )
    ns = fake.retrieve_calls[0]["namespace"]
    assert ns.startswith("agate/chem/personal/")


def test_recall_session_requires_session_id(fake):
    out = _invoke({"idp_token": "t", "op": "recall", "tier": "session"})
    assert out["status"] == 403
    assert fake.retrieve_calls == []


def test_recall_rejects_unknown_tier(fake):
    out = _invoke({"idp_token": "t", "op": "recall", "tier": "global"})
    assert out["status"] == 403


def test_recall_shared_rejected_when_unscoped(fake, monkeypatch):
    # An unscoped session has no shared tier (namespaces_for omits it) -> fail closed.
    monkeypatch.setattr(h, "validate_idp_token", lambda token: dict(_UNSCOPED_CLAIMS))
    out = _invoke({"idp_token": "t", "op": "recall", "tier": "shared"})
    assert out["status"] == 403
    assert fake.retrieve_calls == []


def test_recall_shared_allowed_when_scoped(fake):
    out = _invoke({"idp_token": "t", "op": "recall", "tier": "shared"})
    assert out["status"] == 200
    assert fake.retrieve_calls[0]["namespace"] == "agate/chem/shared/chemistry/chem-101/"


# --- fail closed -------------------------------------------------------------


def test_missing_token_fails_closed(fake):
    out = _invoke({"op": "recall", "tier": "personal"})
    assert out["status"] == 403
    assert fake.retrieve_calls == []


def test_unknown_op_fails_closed(fake):
    out = _invoke({"idp_token": "t", "op": "delete-everything"})
    assert out["status"] == 403


def test_no_memory_id_configured_fails_closed(fake, monkeypatch):
    monkeypatch.setattr(h, "MEMORY_ID", "")
    out = _invoke({"idp_token": "t", "op": "recall", "tier": "personal"})
    assert out["status"] == 403
