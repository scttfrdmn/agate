"""Unit tests for the broker-proxied vector retrieval Lambda (#84). No live AWS.

Proves the boundary behaviours: scope/tenant/index come ONLY from the verified token
(injected request fields are ignored), the injected filter is exactly
`scope_filter(retrieval_nodes(...))`, the query hits `agate-{verified_tenant}`, and a
bad/un-scopable token returns NO results (fail closed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agate.rag import retrieval_nodes, scope_filter  # noqa: E402
from infra.functions.retrieval import handler as retrieval  # noqa: E402


class _FakeSts:
    def __init__(self):
        self.last_call = None

    def assume_role(self, **kwargs):
        self.last_call = kwargs
        return {
            "Credentials": {
                "AccessKeyId": "ASIAFAKE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
            }
        }


class _FakeVectors:
    """Captures the query_vectors call and returns one in-scope chunk."""

    def __init__(self):
        self.last_query = None

    def query_vectors(self, **kwargs):
        self.last_query = kwargs
        return {
            "vectors": [
                {
                    "key": "chem/chemistry/chem-101/a#0",
                    "metadata": {
                        "text": "covalent bonds",
                        "source_key": "chem/chemistry/chem-101/a",
                    },
                    "distance": 0.12,
                },
                {"key": "no-text#1", "metadata": {}, "distance": 0.9},  # dropped (no text)
            ]
        }


@pytest.fixture
def stub(monkeypatch):
    sts, vectors = _FakeSts(), _FakeVectors()
    monkeypatch.setattr(retrieval, "_sts", sts)
    monkeypatch.setattr(
        retrieval, "VECTOR_READER_ROLE_ARN", "arn:aws:iam::123:role/agate-vector-reader"
    )
    monkeypatch.setattr(retrieval, "VECTOR_BUCKET", "agate-vectors-123-us-east-1")
    # The verifier: the token string IS the JSON claims (as in broker/admin tests).
    monkeypatch.setattr(retrieval, "config_from_env", lambda: {})

    def fake_verify(token, **_cfg):
        if not token:
            from agate.jwt_verify import TokenError

            raise TokenError("empty")
        return json.loads(token)

    monkeypatch.setattr(retrieval, "verify_token", fake_verify)
    # Assume-role returns our capturing vectors client; embedding is a fixed vector.
    monkeypatch.setattr(retrieval, "embed_query", lambda q: [0.1, 0.2, 0.3])
    monkeypatch.setattr(retrieval.boto3, "client", lambda *a, **k: vectors, raising=False)
    return sts, vectors


def _event(claims: dict, **body) -> dict:
    return {"body": json.dumps({"idp_token": json.dumps(claims), **body})}


def test_query_hits_verified_tenant_index_with_injected_filter(stub):
    _sts, vectors = stub
    claims = {"sub": "u1", "tenant": "chem", "role": "member", "data_scope": "chemistry/chem-101"}
    resp = retrieval.handler(_event(claims, query="bonds"), None)
    assert resp["statusCode"] == 200
    # Index derived from the verified tenant.
    assert vectors.last_query["indexName"] == "agate-chem"
    assert vectors.last_query["vectorBucketName"] == "agate-vectors-123-us-east-1"
    # Filter is exactly what the pure helpers produce from the token's scope.
    expected = scope_filter(retrieval_nodes("chemistry/chem-101", ()))
    assert vectors.last_query["filter"] == expected
    # Only the chunk with text survives.
    body = json.loads(resp["body"])
    assert [c["key"] for c in body["chunks"]] == ["chem/chemistry/chem-101/a#0"]
    assert body["chunks"][0]["sourceKey"] == "chem/chemistry/chem-101/a"


def test_assumes_vector_reader_with_token_tags(stub):
    sts, _vectors = stub
    claims = {"sub": "u1", "tenant": "chem", "role": "member", "data_scope": "chemistry"}
    retrieval.handler(_event(claims, query="x"), None)
    assert sts.last_call["RoleArn"].endswith("agate-vector-reader")
    sent = {t["Key"]: t["Value"] for t in sts.last_call["Tags"]}
    assert sent["agate:tenant"] == "chem"
    assert sent["agate:scope"] == "chemistry"
    # Tenant encoded in the session name (#79 attribution).
    assert sts.last_call["RoleSessionName"] == "chem@u1"


def test_injected_tenant_scope_filter_fields_are_ignored(stub):
    # The crux: a client cannot widen its scope by supplying tenant/scope/filter/index.
    _sts, vectors = stub
    claims = {"sub": "u1", "tenant": "chem", "role": "member", "data_scope": "chemistry/chem-101"}
    resp = retrieval.handler(
        _event(
            claims,
            query="x",
            tenant="psych",  # attacker tries another tenant
            scope="chemistry/chem-202",  # ... and a sibling scope
            filter={},  # ... and an empty (wide) filter
            index="agate-psych",
        ),
        None,
    )
    assert resp["statusCode"] == 200
    # Still the verified tenant + the token-derived filter — body fields ignored.
    assert vectors.last_query["indexName"] == "agate-chem"
    assert vectors.last_query["filter"] == scope_filter(retrieval_nodes("chemistry/chem-101", ()))


def test_unconfined_session_uses_courses_only(stub):
    # No data_scope, courses present -> retrieval_nodes == courses (tenant-wide + courses).
    _sts, vectors = stub
    claims = {"sub": "u1", "tenant": "chem", "role": "member", "courses": "chem-101,chem-202"}
    retrieval.handler(_event(claims, query="x"), None)
    assert vectors.last_query["filter"] == scope_filter(
        retrieval_nodes("", ("chem-101", "chem-202"))
    )


def test_bad_token_returns_no_results(stub):
    _sts, vectors = stub
    resp = retrieval.handler(_event_blank(), None)
    assert resp["statusCode"] == 403
    assert vectors.last_query is None  # no query ran


def _event_blank() -> dict:
    return {"body": json.dumps({"idp_token": "", "query": "x"})}


def test_missing_query_rejected(stub):
    _sts, vectors = stub
    claims = {"sub": "u1", "tenant": "chem", "role": "member"}
    resp = retrieval.handler(_event(claims), None)
    assert resp["statusCode"] == 403
    assert vectors.last_query is None


def test_unscopable_claims_fail_closed(stub):
    # No tenant -> claims_to_tags raises ClaimsError -> no results, no query.
    _sts, vectors = stub
    resp = retrieval.handler(_event({"sub": "u1", "role": "member"}, query="x"), None)
    assert resp["statusCode"] == 403
    assert vectors.last_query is None


def test_top_k_clamped(stub):
    _sts, vectors = stub
    claims = {"sub": "u1", "tenant": "chem", "role": "member"}
    retrieval.handler(_event(claims, query="x", top_k=9999), None)
    assert vectors.last_query["topK"] == retrieval.MAX_TOP_K
