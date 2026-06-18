"""CDK synth assertions for the AuthoringStack (#117). No deploy.

Asserts the bounded-menu endpoint synthesizes as least-privilege: a Lambda behind an
IAM-authed Function URL with NO Bedrock and NO S3/STS grant — it only reads the bounded menu
and runs the pure compiler clamp (the author's authority is the verified token). This is the
distinguishing property vs the drafting (Bedrock) and deploy (S3 write) endpoints.
"""

from __future__ import annotations

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402
from infra.stacks.authoring import AuthoringStack  # noqa: E402

_ENV = cdk.Environment(account="111122223333", region="us-east-1")


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    return assertions.Template.from_stack(AuthoringStack(app, "agate-authoring-synth", env=_ENV))


def test_lambda_and_iam_authed_function_url(template):
    fns = template.find_resources("AWS::Lambda::Function")
    memfn = [
        f
        for f in fns.values()
        if f["Properties"].get("Handler") == "infra.functions.authoring.handler.handler"
    ]
    assert len(memfn) == 1
    urls = template.find_resources("AWS::Lambda::Url")
    assert len(urls) == 1
    props = list(urls.values())[0]["Properties"]
    assert props["AuthType"] == "AWS_IAM"
    # CORS must be present (the SPA calls this cross-origin from CloudFront; without it the
    # browser preflight gets no Access-Control-Allow-Origin and fails "Failed to fetch").
    cors = props["Cors"]
    assert "POST" in cors["AllowMethods"]
    assert "authorization" in cors["AllowHeaders"]  # SigV4-signed request header


def test_no_bedrock_or_data_grant_least_privilege(template):
    # The distinguishing property: this endpoint neither invokes a model nor writes data, so
    # its role must carry NO bedrock / s3 / sts:AssumeRole grant — only the default logs.
    pols = template.find_resources("AWS::IAM::Policy")
    actions = [
        a
        for p in pols.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    ]
    blob = " ".join(actions)
    assert "bedrock:" not in blob
    assert "s3:" not in blob
    assert "sts:AssumeRole" not in blob
