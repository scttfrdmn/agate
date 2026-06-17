"""Live proof: room read/write is tenant-fenced by the credential (#116).

Runs the generated `room_rw_policy` through IAM's simulator with `agate:` principal tags + a
target `_rooms/` object key, proving the assumed rooms role can read/write ONLY under its own
tenant — never another's. (A room object is tenant-rooted metadata; the scope fences live in
`rooms.effective_member_tags` + the transcript SavedSession + handler membership, not the
object key.) Read-only (`iam:SimulateCustomPolicy`); skipped without AWS creds.

Run explicitly:  AWS_PROFILE=aws uv run pytest -m aws tests/test_proof_room_write.py -v
"""

from __future__ import annotations

import json

import pytest
from agate.names import tag_key
from policy.generate import room_rw_policy

REGION = "us-east-1"
BUCKET = "agate-docs-111122223333-us-east-1"
_POLICY = room_rw_policy(bucket=BUCKET)

boto3 = pytest.importorskip("boto3")


@pytest.fixture(scope="module")
def iam_client():
    client = boto3.client("iam", region_name=REGION)
    try:
        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable AWS credentials for live simulation: {exc}")
    return client


def _eval(iam_client, *, action: str, key: str, tenant: str | None) -> str:
    ctx = []
    if tenant is not None:
        ctx.append(
            {
                "ContextKeyName": f"aws:PrincipalTag/{tag_key('tenant')}",
                "ContextKeyType": "string",
                "ContextKeyValues": [tenant],
            }
        )
    resp = iam_client.simulate_custom_policy(
        PolicyInputList=[json.dumps(_POLICY)],
        ActionNames=[action],
        ResourceArns=[f"arn:aws:s3:::{BUCKET}/{key}"],
        ContextEntries=ctx,
    )
    return resp["EvaluationResults"][0]["EvalDecision"]


@pytest.mark.aws
def test_reads_and_writes_own_tenant_room(iam_client):
    for action in ("s3:GetObject", "s3:PutObject"):
        d = _eval(iam_client, action=action, key="uni/_rooms/lab-1.json", tenant="uni")
        assert d == "allowed"


@pytest.mark.aws
def test_cannot_touch_another_tenants_room(iam_client):
    for action in ("s3:GetObject", "s3:PutObject"):
        d = _eval(iam_client, action=action, key="other/_rooms/lab-1.json", tenant="uni")
        assert d in ("implicitDeny", "explicitDeny")


@pytest.mark.aws
def test_no_tenant_tag_denies_room_access(iam_client):
    d = _eval(iam_client, action="s3:PutObject", key="uni/_rooms/lab-1.json", tenant=None)
    assert d in ("implicitDeny", "explicitDeny")
