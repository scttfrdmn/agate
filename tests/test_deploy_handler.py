"""Unit tests for the deploy-on-confirm endpoint (#118 last slice). No AWS — STS/S3 stubbed.

The load-bearing assertions: the endpoint RE-CLAMPS the confirmed spec against the VERIFIED
token (never trusting the echoed spec as authority), keys the record under the re-clamped
tenant/scope, assumes a tag-scoped role to write, and fails closed. A tampered/over-broad spec
is clamped or rejected exactly as a fresh draft.
"""

from __future__ import annotations

import json

import pytest
from infra.functions.deploy import handler as h


def _claims(tenant="uni", scope="chemistry/chem-101"):
    return {
        "sub": "prof",
        "affiliation": "researcher",
        "tenant": tenant,
        "data_scope": scope,
        "grant": True,
    }


_SPEC = {
    "agent": "paper-sweep",
    "description": "summarize new papers",
    "role": "researcher",
    "scope": "chemistry/chem-101",
    "reasoning": "lit-review",
    "tools": ["library-search"],
}


class _FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)
        return {}


@pytest.fixture
def stub(monkeypatch):
    s3 = _FakeS3()
    monkeypatch.setattr(h, "DOCS_BUCKET", "agate-docs")
    monkeypatch.setattr(h, "DEPLOY_ROLE_ARN", "arn:aws:iam::111122223333:role/agate-deploy")
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims() if tok else _raise())
    monkeypatch.setattr(h, "_assume_writer", lambda tags, subject: s3)
    monkeypatch.setattr(h, "_now_iso", lambda: "2026-06-17T00:00:00Z")
    return s3


def _raise():
    raise h.DeployError("missing idp_token")


def _invoke(req: dict) -> dict:
    resp = h.handler({"body": json.dumps(req)}, None)
    return {"status": resp["statusCode"], "body": json.loads(resp["body"])}


# --- happy path: persist under the re-clamped scope ------------------------


def test_confirm_persists_agent_record(stub):
    out = _invoke({"idp_token": "t", "spec": _SPEC})
    assert out["status"] == 200
    assert out["body"]["ok"] is True
    assert out["body"]["agent_id"] == "uni/paper-sweep"
    assert out["body"]["key"] == "uni/chemistry/chem-101/_agents/paper-sweep.json"
    # exactly one object written, under the re-clamped key
    assert len(stub.puts) == 1
    assert stub.puts[0]["Key"] == "uni/chemistry/chem-101/_agents/paper-sweep.json"
    body = json.loads(stub.puts[0]["Body"].decode())
    assert body["created_by"] == "uni@prof"  # verified, not client-claimed
    assert body["spec"] == _SPEC


# --- the headline: the echoed spec is NOT trusted as authority -------------


def test_over_broad_spec_is_reclamped_not_persisted_as_is(stub, monkeypatch):
    # The client echoes a spec scoped to ANOTHER tenant's tree; re-clamp rejects it (disjoint).
    out = _invoke({"idp_token": "t", "spec": {**_SPEC, "scope": "physics"}})
    assert out["status"] == 200
    assert out["body"]["ok"] is False
    assert stub.puts == []  # nothing persisted


def test_broader_scope_clamps_down_to_author(stub, monkeypatch):
    # Author at chemistry/chem-101 echoes the broader 'chemistry' -> record keyed at the
    # author's narrower scope, never the broader one the body asked for.
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims(scope="chemistry/chem-101"))
    out = _invoke({"idp_token": "t", "spec": {**_SPEC, "scope": "chemistry"}})
    assert out["body"]["ok"] is True
    assert out["body"]["key"] == "uni/chemistry/chem-101/_agents/paper-sweep.json"


def test_writer_assumed_with_verified_tenant(stub, monkeypatch):
    # The write must go through the assume-with-tags path (the tag-scoped role), not the
    # Lambda's own role — capture the tags passed.
    seen = {}
    monkeypatch.setattr(
        h,
        "_assume_writer",
        lambda tags, subject: seen.update(tenant=tags.tenant, sub=subject) or stub,
    )
    _invoke({"idp_token": "t", "spec": _SPEC})
    assert seen["tenant"] == "uni"
    assert seen["sub"] == "prof"


# --- fail closed ------------------------------------------------------------


def test_missing_token_is_403(stub):
    out = _invoke({"spec": _SPEC})
    assert out["status"] == 403
    assert stub.puts == []


def test_missing_spec_is_403(stub):
    out = _invoke({"idp_token": "t"})
    assert out["status"] == 403
    assert stub.puts == []


def test_invalid_spec_fails_closed(stub):
    # A spec parse_spec rejects (unknown tool) -> re-clamp fails -> nothing persisted.
    out = _invoke({"idp_token": "t", "spec": {**_SPEC, "tools": ["no-such-tool"]}})
    assert out["status"] == 200
    assert out["body"]["ok"] is False
    assert stub.puts == []


def test_no_bucket_configured_fails_closed(stub, monkeypatch):
    monkeypatch.setattr(h, "DOCS_BUCKET", "")
    out = _invoke({"idp_token": "t", "spec": _SPEC})
    assert out["status"] == 403
    assert stub.puts == []
