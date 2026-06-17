"""Live proof: created-agent writes are fenced by the same credential (#118 deploy-on-confirm).

Runs the generated `agent_write_policy` through IAM's simulator with `agate:` principal tags +
a target `_agents/` object key, proving the deploy role can write ONLY under its own tenant
(and, when scoped, only its own subtree) — never another tenant's or another scope's. This is
the write-side analogue of the #80/#110 read proofs. Read-only (`iam:SimulateCustomPolicy`);
skipped without AWS creds.

Run explicitly:  AWS_PROFILE=aws uv run pytest -m aws tests/test_proof_agent_write.py -v
"""

from __future__ import annotations

import json

import pytest
from agate.names import tag_key
from policy.generate import agent_write_policy

REGION = "us-east-1"
BUCKET = "agate-docs-111122223333-us-east-1"
_POLICY = agent_write_policy(bucket=BUCKET)

boto3 = pytest.importorskip("boto3")


@pytest.fixture(scope="module")
def iam_client():
    client = boto3.client("iam", region_name=REGION)
    try:
        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable AWS credentials for live simulation: {exc}")
    return client


def _tags(tenant: str, scope: str | None) -> list[dict]:
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


def _eval(iam_client, *, key: str, tenant: str, scope: str | None) -> str:
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(_POLICY)],
        ActionNames=["s3:PutObject"],
        ResourceArns=[f"arn:aws:s3:::{BUCKET}/{key}"],
        ContextEntries=_tags(tenant, scope),
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


# --- own tenant write allowed -----------------------------------------------


@pytest.mark.aws
def test_unscoped_author_writes_tenant_root_agents(iam_client):
    # The common tenant-wide case: an unscoped session writes {tenant}/_agents/{name}.
    d = _eval(iam_client, key="chem/_agents/paper-sweep.json", tenant="chem", scope=None)
    assert d == "allowed"


@pytest.mark.aws
def test_scoped_author_writes_own_subtree_agents(iam_client):
    d = _eval(
        iam_client,
        key="chem/chemistry/chem-101/_agents/paper-sweep.json",
        tenant="chem",
        scope="chemistry/chem-101",
    )
    assert d == "allowed"


# --- cross-tenant denied (the fence) ----------------------------------------


@pytest.mark.aws
def test_cannot_write_another_tenants_agents(iam_client):
    d = _eval(iam_client, key="psych/_agents/evil.json", tenant="chem", scope=None)
    assert d in ("implicitDeny", "explicitDeny")


# --- scoped session is confined to its subtree ------------------------------


@pytest.mark.aws
def test_scoped_session_cannot_write_sibling_scope(iam_client):
    # chem-101-scoped must NOT write chem-202's agents.
    d = _eval(
        iam_client,
        key="chem/chemistry/chem-202/_agents/x.json",
        tenant="chem",
        scope="chemistry/chem-101",
    )
    assert d == "explicitDeny"


@pytest.mark.aws
def test_scoped_session_cannot_write_tenant_root(iam_client):
    # A scoped session may not escape UP to the tenant-root _agents/ (outside its subtree).
    d = _eval(iam_client, key="chem/_agents/x.json", tenant="chem", scope="chemistry/chem-101")
    assert d == "explicitDeny"


# --- fail closed: no tenant tag ---------------------------------------------


@pytest.mark.aws
def test_no_tenant_tag_denies_write(iam_client):
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(_POLICY)],
        ActionNames=["s3:PutObject"],
        ResourceArns=[f"arn:aws:s3:::{BUCKET}/chem/_agents/x.json"],
        ContextEntries=[],  # no principal tags at all
    )
    assert resp["EvaluationResults"][0]["EvalDecision"] in ("implicitDeny", "explicitDeny")
