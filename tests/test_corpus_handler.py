"""Unit tests for the corpus endpoint (#191). No AWS — STS/S3 stubbed.

The load-bearing assertions: the key/prefix come from the VERIFIED token's tenant+scope
(never a body field), upload PUTs under that fence, list enumerates only that subtree and
hides reserved namespaces, and everything fails closed on a bad token.
"""

from __future__ import annotations

import base64
import datetime as dt
import json

import pytest
from infra.functions.corpus import handler as h


def _claims(tenant="chem", scope="chemistry/chem-101"):
    return {"sub": "stu", "affiliation": "student", "tenant": tenant, "data_scope": scope}


class _FakeS3:
    def __init__(self, contents=None):
        self.puts = []
        self.lists = []
        self._contents = contents or []

    def put_object(self, **kw):
        self.puts.append(kw)
        return {}

    def list_objects_v2(self, **kw):
        self.lists.append(kw)
        return {"Contents": self._contents, "IsTruncated": False}


@pytest.fixture
def stub(monkeypatch):
    s3 = _FakeS3()
    monkeypatch.setattr(h, "DOCS_BUCKET", "agate-docs")
    monkeypatch.setattr(h, "CORPUS_ROLE_ARN", "arn:aws:iam::111122223333:role/agate-corpus")
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims() if tok else _raise())
    monkeypatch.setattr(h, "_assume_corpus_role", lambda tags, subject: s3)
    return s3


def _raise():
    raise h.CorpusError("missing idp_token")


def _invoke(req: dict) -> dict:
    resp = h.handler({"body": json.dumps(req)}, None)
    return {"status": resp["statusCode"], "body": json.loads(resp["body"])}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# --- upload -----------------------------------------------------------------


def test_upload_puts_under_verified_scope(stub):
    out = _invoke(
        {"idp_token": "t", "action": "upload", "filename": "notes.txt", "content": _b64("hi")}
    )
    assert out["status"] == 200 and out["body"]["ok"] is True
    assert out["body"]["key"] == "chem/chemistry/chem-101/notes.txt"
    assert len(stub.puts) == 1
    assert stub.puts[0]["Key"] == "chem/chemistry/chem-101/notes.txt"
    assert stub.puts[0]["Body"] == b"hi"


def test_upload_ignores_a_client_supplied_key_or_tenant(stub):
    # A body tenant/scope/key field must NOT influence where the doc lands.
    out = _invoke(
        {
            "idp_token": "t",
            "action": "upload",
            "filename": "x.txt",
            "content": _b64("y"),
            "tenant": "other",
            "scope": "physics",
            "key": "other/physics/evil.txt",
        }
    )
    assert out["body"]["key"] == "chem/chemistry/chem-101/x.txt"  # verified fence, not the body


def test_upload_sanitises_filename_path(stub):
    out = _invoke(
        {"idp_token": "t", "action": "upload", "filename": "../../etc/passwd", "content": _b64("z")}
    )
    assert out["body"]["key"] == "chem/chemistry/chem-101/passwd"


def test_upload_rejects_oversize(stub, monkeypatch):
    monkeypatch.setattr(h, "MAX_UPLOAD_BYTES", 4)
    out = _invoke(
        {"idp_token": "t", "action": "upload", "filename": "big.txt", "content": _b64("toolong")}
    )
    assert out["status"] == 403
    assert stub.puts == []


def test_upload_rejects_bad_base64(stub):
    out = _invoke(
        {"idp_token": "t", "action": "upload", "filename": "f.txt", "content": "not base64!!"}
    )
    assert out["status"] == 403
    assert stub.puts == []


# --- list -------------------------------------------------------------------


def test_list_enumerates_scope_subtree(monkeypatch):
    when = dt.datetime(2026, 6, 25, tzinfo=dt.UTC)
    contents = [
        {"Key": "chem/chemistry/chem-101/notes.txt", "Size": 12, "LastModified": when},
        {"Key": "chem/chemistry/chem-101/paper.pdf", "Size": 99, "LastModified": when},
        {"Key": "chem/chemistry/chem-101/_agents/x.json", "Size": 5, "LastModified": when},
        {"Key": "chem/chemistry/chem-101/", "Size": 0, "LastModified": when},  # folder marker
    ]
    s3 = _FakeS3(contents)
    monkeypatch.setattr(h, "DOCS_BUCKET", "agate-docs")
    monkeypatch.setattr(h, "CORPUS_ROLE_ARN", "arn:aws:iam::111122223333:role/agate-corpus")
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims())
    monkeypatch.setattr(h, "_assume_corpus_role", lambda tags, subject: s3)

    out = _invoke({"idp_token": "t", "action": "list"})
    assert out["status"] == 200
    names = [d["name"] for d in out["body"]["documents"]]
    assert names == ["notes.txt", "paper.pdf"]  # _agents/ and the folder marker excluded
    # listed with the verified prefix
    assert s3.lists[0]["Prefix"] == "chem/chemistry/chem-101/"


# --- fail closed ------------------------------------------------------------


def test_missing_token_is_403(stub):
    out = _invoke({"action": "upload", "filename": "f.txt", "content": _b64("x")})
    assert out["status"] == 403
    assert stub.puts == []


def test_unknown_action_is_403(stub):
    out = _invoke({"idp_token": "t", "action": "delete"})
    assert out["status"] == 403


def test_no_bucket_configured_fails_closed(stub, monkeypatch):
    monkeypatch.setattr(h, "DOCS_BUCKET", "")
    out = _invoke({"idp_token": "t", "action": "list"})
    assert out["status"] == 403
