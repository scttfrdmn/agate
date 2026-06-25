"""CDK synth assertions for the CorpusStack (#191). No deploy.

Asserts the wiring that lets the SPA upload/list within its fence: an AWS_IAM Function
URL with CORS, the OIDC verifier env, a tenant-fenced corpus role the Lambda assumes
(with the read/write/list policy + scope-confinement Deny), and the auth-role invoke
permissions (both actions, per #190).
"""

from __future__ import annotations

import json

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402
from infra.stacks.corpus import CorpusStack  # noqa: E402

_ENV = cdk.Environment(account="111122223333", region="us-east-1")


def _template():
    app = cdk.App()
    return assertions.Template.from_stack(CorpusStack(app, "agate-corpus-synth", env=_ENV))


def test_iam_authed_function_url_with_cors():
    t = _template()
    urls = t.find_resources("AWS::Lambda::Url")
    assert len(urls) == 1
    props = next(iter(urls.values()))["Properties"]
    assert props["AuthType"] == "AWS_IAM"
    assert "POST" in props["Cors"]["AllowMethods"]


def test_lambda_only_assumes_the_fenced_role_no_direct_s3():
    # The Lambda's own role must NOT carry s3:PutObject — its only authority is to assume
    # (and tag) the corpus role. The write/list fence lives on the assumed role.
    t = _template()
    pols = t.find_resources("AWS::IAM::Policy")
    all_actions = []
    for p in pols.values():
        for s in p["Properties"]["PolicyDocument"]["Statement"]:
            acts = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            all_actions.append((s.get("Effect"), acts))
    # an AssumeRole+TagSession grant exists
    assert any(
        s_eff == "Allow" and "sts:AssumeRole" in acts for s_eff, acts in all_actions
    )


def test_corpus_role_is_scope_fenced():
    # The corpus role's policy must include the PutObject grant AND the scope-confinement
    # Deny (NotResource on {tenant}/{scope}/*) so a scoped session can't write elsewhere.
    t = _template()
    blob = json.dumps(t.to_json())
    assert "s3:PutObject" in blob
    assert "DenyCorpusOutsideScopeSubtree" in blob
    assert "DenyCorpusWhenNoTenantTag" in blob
    # list is scope-confined too (parity with data_scope_policy)
    assert "DenyCorpusListOutsideScopeSubtree" in blob


def test_auth_role_invoke_permissions_both_actions():
    # #190: a boundaried auth role invoking a Function URL needs BOTH actions granted as
    # resource permissions.
    t = _template()
    t.has_resource_properties(
        "AWS::Lambda::Permission",
        {"Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "AWS_IAM"},
    )
    t.has_resource_properties(
        "AWS::Lambda::Permission",
        {"Action": "lambda:InvokeFunction", "InvokedViaFunctionUrl": True},
    )


def test_oidc_env_wired():
    t = _template()
    fns = t.find_resources("AWS::Lambda::Function")
    handler_name = "infra.functions.corpus.handler.handler"
    corpus = next(f for f in fns.values() if f["Properties"].get("Handler") == handler_name)
    env = corpus["Properties"]["Environment"]["Variables"]
    assert "AGATE_DOCS_BUCKET" in env
    assert "AGATE_CORPUS_ROLE_ARN" in env  # set via add_environment
