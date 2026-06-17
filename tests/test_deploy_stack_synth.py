"""CDK synth assertions for the DeployStack (#118 deploy-on-confirm). No deploy.

Asserts the endpoint synthesizes with the #130-pattern write fence: a Lambda behind an
IAM-authed Function URL whose OWN role can only `sts:AssumeRole` a separate tenant-fenced role
(carrying the `_agents/` PutObject policy) — so the writing principal carries the verified
agate: tags the bucket policy fences, not the un-tagged Lambda role.
"""

from __future__ import annotations

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402
from infra.stacks.deploy import DeployStack  # noqa: E402

_ENV = cdk.Environment(account="111122223333", region="us-east-1")


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    return assertions.Template.from_stack(DeployStack(app, "agate-deploy-synth", env=_ENV))


def test_lambda_and_iam_authed_function_url(template):
    fns = template.find_resources("AWS::Lambda::Function")
    memfn = [
        f
        for f in fns.values()
        if f["Properties"].get("Handler") == "infra.functions.deploy.handler.handler"
    ]
    assert len(memfn) == 1
    urls = template.find_resources("AWS::Lambda::Url")
    assert len(urls) == 1
    assert list(urls.values())[0]["Properties"]["AuthType"] == "AWS_IAM"


def test_write_fence_lives_on_a_distinct_assumed_role(template):
    # The #130 discipline: the PutObject fence must be on a SEPARATE role (carrying the agate:
    # tags), and the Lambda's own role must only be able to AssumeRole+TagSession it.
    pols = template.find_resources("AWS::IAM::Policy")
    # the _agents/ PutObject fence is present
    put_stmts = [
        s
        for p in pols.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if s.get("Sid") == "PutOwnTenantAgents"
    ]
    assert len(put_stmts) == 1
    assert put_stmts[0]["Action"] == "s3:PutObject"
    assert "_agents" in str(put_stmts[0]["Resource"])
    # the Lambda can assume + tag a role (the write role), not write S3 directly
    assume = [
        s
        for p in pols.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "sts:AssumeRole"
        in (s.get("Action") if isinstance(s.get("Action"), list) else [s.get("Action")])
    ]
    assert assume, "the Lambda must assume the tenant-fenced write role"
    assert any("sts:TagSession" in (s.get("Action") or []) for s in assume)
    # two roles: the Lambda exec role + the assumable write role
    assert len(template.find_resources("AWS::IAM::Role")) >= 2


def test_env_wires_bucket_and_deploy_role(template):
    fns = template.find_resources("AWS::Lambda::Function")
    memfn = next(
        f
        for f in fns.values()
        if f["Properties"].get("Handler") == "infra.functions.deploy.handler.handler"
    )
    env = memfn["Properties"]["Environment"]["Variables"]
    assert "AGATE_DOCS_BUCKET" in env
    assert "AGATE_AGENT_DEPLOY_ROLE_ARN" in env
