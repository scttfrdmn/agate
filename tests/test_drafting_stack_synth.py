"""CDK synth assertions for the DraftingStack (#118b). No deploy.

Asserts the drafting endpoint synthesizes: a Lambda behind an IAM-authed Function URL, with a
Bedrock invoke grant scoped to the entitled-model ARNs (NOT `*`) — the per-session tier is
enforced in the handler, but the IAM bound must still be the entitled superset, not unbounded.
"""

from __future__ import annotations

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402
from infra.stacks.drafting import DraftingStack  # noqa: E402

_ENV = cdk.Environment(account="111122223333", region="us-east-1")


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    return assertions.Template.from_stack(DraftingStack(app, "agate-drafting-synth", env=_ENV))


def test_lambda_and_iam_authed_function_url(template):
    fns = template.find_resources("AWS::Lambda::Function")
    memfn = [
        f
        for f in fns.values()
        if f["Properties"].get("Handler") == "infra.functions.drafting.handler.handler"
    ]
    assert len(memfn) == 1
    urls = template.find_resources("AWS::Lambda::Url")
    assert len(urls) == 1
    assert list(urls.values())[0]["Properties"]["AuthType"] == "AWS_IAM"


def test_bedrock_grant_is_scoped_to_entitled_models_not_wildcard(template):
    pols = template.find_resources("AWS::IAM::Policy")
    grants = [
        s
        for p in pols.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if s.get("Sid") == "BedrockDraftInvoke"
    ]
    assert len(grants) == 1
    actions = grants[0]["Action"]
    assert "bedrock:Converse" in actions
    res = grants[0]["Resource"]
    res = [res] if isinstance(res, str) else res
    # scoped to foundation-model ARNs, never a bare "*"
    assert res and all(r != "*" for r in res)
    assert any("foundation-model" in str(r) for r in res)


def test_max_tokens_env_wired(template):
    fns = template.find_resources("AWS::Lambda::Function")
    memfn = next(
        f
        for f in fns.values()
        if f["Properties"].get("Handler") == "infra.functions.drafting.handler.handler"
    )
    assert memfn["Properties"]["Environment"]["Variables"]["AGATE_DRAFT_MAX_TOKENS"] == "1024"
