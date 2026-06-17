"""CDK synth assertions for the RoomsStack (#116). No deploy.

Asserts the rooms endpoint synthesizes with the #130-pattern fence: a Lambda behind an
IAM-authed Function URL whose OWN role can only `sts:AssumeRole` a separate tenant-fenced
room role (carrying the `_rooms/` Get+Put policy) + read/write the spend/budget DDB rows for the
per-member budget gate. The room write fence lives on the assumed role, not the Lambda role.
"""

from __future__ import annotations

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402
from infra.stacks.rooms import RoomsStack  # noqa: E402

_ENV = cdk.Environment(account="111122223333", region="us-east-1")


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    return assertions.Template.from_stack(RoomsStack(app, "agate-rooms-synth", env=_ENV))


def test_lambda_and_iam_authed_function_url(template):
    fns = template.find_resources("AWS::Lambda::Function")
    memfn = [
        f
        for f in fns.values()
        if f["Properties"].get("Handler") == "infra.functions.rooms.handler.handler"
    ]
    assert len(memfn) == 1
    urls = template.find_resources("AWS::Lambda::Url")
    assert len(urls) == 1
    assert list(urls.values())[0]["Properties"]["AuthType"] == "AWS_IAM"


def test_room_fence_on_a_distinct_assumed_role(template):
    pols = template.find_resources("AWS::IAM::Policy")
    rw = [
        s
        for p in pols.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if s.get("Sid") == "RwOwnTenantRooms"
    ]
    assert len(rw) == 1
    assert "_rooms" in str(rw[0]["Resource"])
    # the Lambda assumes + tags a role (the room role), not touching the room object directly
    assume = [
        s
        for p in pols.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "sts:AssumeRole"
        in (s.get("Action") if isinstance(s.get("Action"), list) else [s.get("Action")])
    ]
    assert assume
    assert any("sts:TagSession" in (s.get("Action") or []) for s in assume)
    # two roles: the Lambda exec role + the assumable room role
    assert len(template.find_resources("AWS::IAM::Role")) >= 2


def test_budget_gate_and_debit_ddb_grant(template):
    pols = template.find_resources("AWS::IAM::Policy")
    ddb = [
        s
        for p in pols.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if s.get("Sid") == "RoomBudgetGateAndDebit"
    ]
    assert len(ddb) == 1
    assert set(ddb[0]["Action"]) == {"dynamodb:GetItem", "dynamodb:UpdateItem"}


def test_env_wires_bucket_role_and_tables(template):
    fns = template.find_resources("AWS::Lambda::Function")
    memfn = next(
        f
        for f in fns.values()
        if f["Properties"].get("Handler") == "infra.functions.rooms.handler.handler"
    )
    env = memfn["Properties"]["Environment"]["Variables"]
    assert "AGATE_DOCS_BUCKET" in env
    assert "AGATE_ROOM_ROLE_ARN" in env
    assert "AGATE_SPEND_TABLE" in env
    assert "AGATE_BUDGET_TABLE" in env
