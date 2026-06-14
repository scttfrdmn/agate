"""Phase 3 end-to-end proof: tenant-scoped S3 Vectors retrieval (design §12).

The FERPA-critical claim (security memo §6): a CHEM-101-scoped session can query
ONLY its own tenant's index. This takes the SAME generated data-scope policy the
Phase 1 identity stack attaches and runs it through IAM's simulator with an
`agate:tenant` principal tag and a target index carrying its own `agate:tenant`
resource tag, asserting:

  * same-tenant index  -> QueryVectors ALLOWED
  * cross-tenant index -> QueryVectors DENIED

Like the Phase 1 model-scope proof this uses iam:SimulateCustomPolicy — read-only,
deterministic, no deployed resources. Skipped without AWS creds.

Run explicitly:  AWS_PROFILE=aws uv run pytest -m aws tests/test_proof_tenant_isolation.py -v
"""

from __future__ import annotations

import json

import pytest
from agate.names import tag_key
from policy.generate import data_scope_policy

REGION = "us-east-1"

boto3 = pytest.importorskip("boto3")


@pytest.fixture(scope="module")
def iam_client():
    client = boto3.client("iam", region_name=REGION)
    try:
        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable AWS credentials for live simulation: {exc}")
    return client


def _simulate_query(iam_client, *, session_tenant: str, index_tenant: str) -> str:
    """Eval s3vectors:QueryVectors for a session in `session_tenant` against an
    index tagged with `index_tenant`."""
    policy_json = json.dumps(data_scope_policy())
    # A representative S3 Vectors index ARN (the resource the policy guards).
    index_arn = (
        f"arn:aws:s3vectors:{REGION}:111122223333:bucket/agate-vectors/index/agate-{index_tenant}"
    )
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[policy_json],
        ActionNames=["s3vectors:QueryVectors"],
        ResourceArns=[index_arn],
        ContextEntries=[
            {
                "ContextKeyName": f"aws:PrincipalTag/{tag_key('tenant')}",
                "ContextKeyType": "string",
                "ContextKeyValues": [session_tenant],
            },
            {
                "ContextKeyName": f"aws:ResourceTag/{tag_key('tenant')}",
                "ContextKeyType": "string",
                "ContextKeyValues": [index_tenant],
            },
        ],
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


@pytest.mark.aws
def test_same_tenant_query_allowed(iam_client):
    assert _simulate_query(iam_client, session_tenant="chem", index_tenant="chem") == "allowed"


@pytest.mark.aws
def test_cross_tenant_query_denied(iam_client):
    # The FERPA nightmare case: a chem session must NOT read the psych index.
    decision = _simulate_query(iam_client, session_tenant="chem", index_tenant="psych")
    assert decision in ("implicitDeny", "explicitDeny")


@pytest.mark.aws
def test_other_direction_also_denied(iam_client):
    decision = _simulate_query(iam_client, session_tenant="psych", index_tenant="chem")
    assert decision in ("implicitDeny", "explicitDeny")


# --- S3 document subtree confinement (#80) ----------------------------------
# Proves IAM (not our reading) enforces `agate:scope`: a confined session reads ONLY
# `{tenant}/{scope}/`. Runs the SAME generated policy the identity stack attaches.

_DOCS_BUCKET = "agate-docs-111122223333-us-east-1"


def _principal_tags(session_tenant: str, session_scope: str | None) -> list[dict]:
    entries = [
        {
            "ContextKeyName": f"aws:PrincipalTag/{tag_key('tenant')}",
            "ContextKeyType": "string",
            "ContextKeyValues": [session_tenant],
        }
    ]
    if session_scope is not None:
        entries.append(
            {
                "ContextKeyName": f"aws:PrincipalTag/{tag_key('scope')}",
                "ContextKeyType": "string",
                "ContextKeyValues": [session_scope],
            }
        )
    return entries


def _simulate_s3_get(iam_client, *, session_tenant, object_key, session_scope=None) -> str:
    """Eval s3:GetObject for a session against `s3://{_DOCS_BUCKET}/{object_key}`."""
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(data_scope_policy(bucket=_DOCS_BUCKET))],
        ActionNames=["s3:GetObject"],
        ResourceArns=[f"arn:aws:s3:::{_DOCS_BUCKET}/{object_key}"],
        ContextEntries=_principal_tags(session_tenant, session_scope),
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


def _simulate_s3_list(iam_client, *, session_tenant, prefix, session_scope=None) -> str:
    """Eval s3:ListBucket with an `s3:prefix` for a session against the docs bucket."""
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(data_scope_policy(bucket=_DOCS_BUCKET))],
        ActionNames=["s3:ListBucket"],
        ResourceArns=[f"arn:aws:s3:::{_DOCS_BUCKET}"],
        ContextEntries=[
            *_principal_tags(session_tenant, session_scope),
            {
                "ContextKeyName": "s3:prefix",
                "ContextKeyType": "string",
                "ContextKeyValues": [prefix],
            },
        ],
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


# No scope tag -> today's tenant-wide behaviour (NO REGRESSION).


@pytest.mark.aws
def test_unscoped_session_reads_tenant_root_doc(iam_client):
    assert (
        _simulate_s3_get(iam_client, session_tenant="chem", object_key="chem/handbook.pdf")
        == "allowed"
    )


@pytest.mark.aws
def test_unscoped_session_reads_deep_doc(iam_client):
    d = _simulate_s3_get(
        iam_client, session_tenant="chem", object_key="chem/chemistry/chem-101/wk.pdf"
    )
    assert d == "allowed"


# Scoped session -> confined to its subtree.


@pytest.mark.aws
def test_scoped_session_reads_within_subtree(iam_client):
    d = _simulate_s3_get(
        iam_client,
        session_tenant="chem",
        session_scope="chemistry",
        object_key="chem/chemistry/chem-101/wk.pdf",
    )
    assert d == "allowed"


@pytest.mark.aws
def test_scoped_session_denied_sibling_subtree(iam_client):
    # The core new guarantee: a chemistry-scoped session can't read physics docs.
    d = _simulate_s3_get(
        iam_client,
        session_tenant="chem",
        session_scope="chemistry",
        object_key="chem/physics/phys-101/wk.pdf",
    )
    assert d == "explicitDeny"


@pytest.mark.aws
def test_scoped_session_denied_tenant_root_doc(iam_client):
    # Strict containment (per design decision): a scoped session is confined to its
    # subtree and does NOT get tenant-root shared docs.
    d = _simulate_s3_get(
        iam_client, session_tenant="chem", session_scope="chemistry", object_key="chem/handbook.pdf"
    )
    assert d == "explicitDeny"


@pytest.mark.aws
def test_scoped_session_cross_tenant_still_denied(iam_client):
    # The tenant fence must still hold even with a scope tag present.
    d = _simulate_s3_get(
        iam_client,
        session_tenant="chem",
        session_scope="chemistry",
        object_key="psych/chemistry/wk.pdf",
    )
    assert d in ("implicitDeny", "explicitDeny")


@pytest.mark.aws
def test_scoped_list_confined_to_subtree_prefix(iam_client):
    allowed = _simulate_s3_list(
        iam_client, session_tenant="chem", session_scope="chemistry", prefix="chem/chemistry/"
    )
    denied = _simulate_s3_list(
        iam_client, session_tenant="chem", session_scope="chemistry", prefix="chem/physics/"
    )
    assert allowed == "allowed"
    assert denied == "explicitDeny"
