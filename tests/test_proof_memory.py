"""Live proof: cross-session memory is fenced by the same credential (#110, §3, §10.3).

Runs the generated `memory_access_policy` through IAM's simulator with
`agate:` principal tags + a `bedrock-agentcore:namespacePath` resource context, proving
a principal can reach its OWN tenant/scope memory namespaces but NOT another tenant's or
another scope's. Read-only (`iam:SimulateCustomPolicy`); skipped without AWS creds.

Run explicitly:  AWS_PROFILE=aws uv run pytest -m aws tests/test_proof_memory.py -v
"""

from __future__ import annotations

import json

import pytest
from agate.memory import personal_namespace, session_namespace, shared_namespace
from agate.names import tag_key
from agate.tags import SessionTags
from policy.generate import MEMORY_READ_ACTIONS, memory_access_policy

REGION = "us-east-1"
MEMORY_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:memory/agate-mem"

boto3 = pytest.importorskip("boto3")

# A chemistry/chem-101-scoped chem-tenant session.
_TAGS = SessionTags(
    affiliation="student",
    tenant="chem",
    courses=("chem-101",),
    tier="oss",
    scope="chemistry/chem-101",
)
_POLICY = memory_access_policy(memory_arn=MEMORY_ARN)


@pytest.fixture(scope="module")
def iam_client():
    client = boto3.client("iam", region_name=REGION)
    try:
        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable AWS credentials for live simulation: {exc}")
    return client


def _principal_tags(tenant: str, scope: str | None) -> list[dict]:
    entries = [
        {
            "ContextKeyName": f"aws:PrincipalTag/{tag_key('tenant')}",
            "ContextKeyType": "string",
            "ContextKeyValues": [tenant],
        }
    ]
    if scope is not None:
        entries.append(
            {
                "ContextKeyName": f"aws:PrincipalTag/{tag_key('scope')}",
                "ContextKeyType": "string",
                "ContextKeyValues": [scope],
            }
        )
    return entries


def _eval(iam_client, *, namespace_path: str, tenant: str, scope: str | None) -> str:
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(_POLICY)],
        ActionNames=MEMORY_READ_ACTIONS,
        ResourceArns=[MEMORY_ARN],
        ContextEntries=[
            *_principal_tags(tenant, scope),
            {
                "ContextKeyName": "bedrock-agentcore:namespacePath",
                "ContextKeyType": "string",
                "ContextKeyValues": [namespace_path],
            },
        ],
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


# --- own tenant memory allowed ----------------------------------------------


@pytest.mark.aws
def test_reads_own_personal_namespace(iam_client):
    ns = personal_namespace(_TAGS, "alice")
    d = _eval(iam_client, namespace_path=ns, tenant="chem", scope="chemistry/chem-101")
    assert d == "allowed"


@pytest.mark.aws
def test_reads_own_session_namespace(iam_client):
    ns = session_namespace(_TAGS, "alice", "s-1")
    d = _eval(iam_client, namespace_path=ns, tenant="chem", scope="chemistry/chem-101")
    assert d == "allowed"


# --- cross-tenant denied (the FERPA fence) ----------------------------------


@pytest.mark.aws
def test_cannot_read_another_tenants_memory(iam_client):
    # A chem session, presenting another tenant's namespace path -> denied.
    d = _eval(
        iam_client,
        namespace_path="agate/psych/personal/bob-abc/",
        tenant="chem",
        scope="chemistry/chem-101",
    )
    assert d in ("implicitDeny", "explicitDeny")


# --- shared tier: own scope allowed, sibling scope denied -------------------


@pytest.mark.aws
def test_reads_own_shared_scope_memory(iam_client):
    ns = shared_namespace(_TAGS)
    assert ns is not None
    d = _eval(iam_client, namespace_path=ns, tenant="chem", scope="chemistry/chem-101")
    assert d == "allowed"


@pytest.mark.aws
def test_cannot_read_sibling_scope_shared_memory(iam_client):
    # chem-101-scoped session must NOT read chem-202's shared memory.
    d = _eval(
        iam_client,
        namespace_path="agate/chem/shared/chemistry/chem-202/",
        tenant="chem",
        scope="chemistry/chem-101",
    )
    assert d == "explicitDeny"


# --- fail closed: no tenant tag ---------------------------------------------


@pytest.mark.aws
def test_no_tenant_tag_denies_all_memory(iam_client):
    # No tenant principal tag at all -> the guard denies every memory action (fail-closed).
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(_POLICY)],
        ActionNames=MEMORY_READ_ACTIONS,
        ResourceArns=[MEMORY_ARN],
        ContextEntries=[
            {
                "ContextKeyName": "bedrock-agentcore:namespacePath",
                "ContextKeyType": "string",
                "ContextKeyValues": ["agate/chem/personal/alice-abc/"],
            }
        ],
    )
    assert resp["EvaluationResults"][0]["EvalDecision"] in ("implicitDeny", "explicitDeny")
