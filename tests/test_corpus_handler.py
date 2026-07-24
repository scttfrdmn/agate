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


class _NoSuchKey(Exception):
    pass


class _FakeS3:
    def __init__(self, contents=None, objects=None):
        self.puts = []
        self.lists = []
        self._contents = contents or []
        self._objects = objects or {}  # key -> bytes, for get_object

        class _Exc:
            NoSuchKey = _NoSuchKey

        self.exceptions = _Exc()

    def put_object(self, **kw):
        self.puts.append(kw)
        # Make a written notebook loadable in the same test.
        self._objects[kw["Key"]] = kw["Body"]
        return {}

    def list_objects_v2(self, **kw):
        self.lists.append(kw)
        return {"Contents": self._contents, "IsTruncated": False}

    def get_object(self, **kw):
        key = kw["Key"]
        if key not in self._objects:
            raise _NoSuchKey(key)

        class _Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": _Body(self._objects[key])}


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


# --- saved notebooks (#200 slice 4) -----------------------------------------


def test_save_notebook_puts_under_notebooks_namespace(stub):
    out = _invoke(
        {
            "idp_token": "t",
            "action": "save_notebook",
            "notebook_id": "nb1",
            "notebook": {"cells": [{"kind": "code", "prompt": "1+1"}]},
        }
    )
    assert out["status"] == 200
    # Fenced under the VERIFIED tenant/scope, in the _notebooks namespace, as JSON.
    assert stub.puts[0]["Key"] == "chem/chemistry/chem-101/_notebooks/nb1.json"
    assert stub.puts[0]["ContentType"] == "application/json"


def test_save_notebook_ignores_body_tenant(stub):
    _invoke(
        {
            "idp_token": "t",
            "action": "save_notebook",
            "tenant": "victim",
            "notebook_id": "nb1",
            "notebook": {"cells": []},
        }
    )
    assert stub.puts[0]["Key"].startswith("chem/")  # not "victim/"


def test_save_then_load_round_trips(stub):
    nb = {"cells": [{"kind": "prompt", "prompt": "q"}], "name": "My NB"}
    _invoke({"idp_token": "t", "action": "save_notebook", "notebook_id": "nb1", "notebook": nb})
    out = _invoke({"idp_token": "t", "action": "load_notebook", "notebook_id": "nb1"})
    assert out["status"] == 200
    assert out["body"]["notebook"] == nb


def test_load_missing_notebook_is_403(stub):
    out = _invoke({"idp_token": "t", "action": "load_notebook", "notebook_id": "nope"})
    assert out["status"] == 403


def test_list_notebooks_enumerates_prefix(monkeypatch):
    when = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    contents = [
        {"Key": "chem/chemistry/chem-101/_notebooks/a.json", "Size": 10, "LastModified": when},
        {"Key": "chem/chemistry/chem-101/_notebooks/b.json", "Size": 20, "LastModified": when},
    ]
    s3 = _FakeS3(contents)
    monkeypatch.setattr(h, "DOCS_BUCKET", "agate-docs")
    monkeypatch.setattr(h, "CORPUS_ROLE_ARN", "arn:aws:iam::111122223333:role/agate-corpus")
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims())
    monkeypatch.setattr(h, "_assume_corpus_role", lambda tags, subject: s3)
    out = _invoke({"idp_token": "t", "action": "list_notebooks"})
    assert out["status"] == 200
    ids = [n["id"] for n in out["body"]["notebooks"]]
    assert ids == ["a", "b"]
    assert s3.lists[0]["Prefix"] == "chem/chemistry/chem-101/_notebooks/"
