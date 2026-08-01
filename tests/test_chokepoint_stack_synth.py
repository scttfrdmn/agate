"""CDK synth assertions for the ChokepointStack (Tier-1 + Ask routing). No deploy.

Asserts the wiring that lets Tier-0 Ask route through the choke point from the browser:
a Lambda behind an AWS_IAM Function URL WITH CORS (the SPA calls it cross-origin), the OIDC
verifier env, a PINNED exec-role name (so agate-identity can trust it by a constructed ARN
without a cross-stack import), and the assume-the-user's-role grant when an auth role is given.
"""

from __future__ import annotations

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402
from infra.stacks.chokepoint import ChokepointStack  # noqa: E402

_ENV = cdk.Environment(account="111122223333", region="us-east-1")
_AUTH_ROLE = "arn:aws:iam::111122223333:role/agate-authenticated"


def _template(context=None):
    app = cdk.App(context=context or {})
    return assertions.Template.from_stack(ChokepointStack(app, "agate-chokepoint-synth", env=_ENV))


def test_iam_authed_function_url_with_cors():
    t = _template({"site_url": "https://example.cloudfront.net"})
    urls = t.find_resources("AWS::Lambda::Url")
    assert len(urls) == 1
    props = list(urls.values())[0]["Properties"]
    assert props["AuthType"] == "AWS_IAM"
    cors = props["Cors"]
    assert "POST" in cors["AllowMethods"]
    assert "authorization" in cors["AllowHeaders"]  # SigV4 header
    # pinned to the SPA origin when site_url is supplied (not "*")
    assert cors["AllowOrigins"] == ["https://example.cloudfront.net"]


def test_exec_role_name_is_pinned():
    # agate-identity trusts this role by a CONSTRUCTED ARN ({HANDLE}-chokepoint-exec), so the
    # name must be pinned (else the trust can't resolve without a cross-stack import).
    t = _template()
    roles = t.find_resources("AWS::IAM::Role")
    names = [r["Properties"].get("RoleName") for r in roles.values()]
    assert "agate-chokepoint-exec" in names


def test_oidc_env_wired():
    t = _template(
        {
            "cognito_discovery_url": (
                "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_x/.well-known/"
                "openid-configuration"
            ),
            "cognito_audience": "aud123",
        }
    )
    fns = t.find_resources("AWS::Lambda::Function")
    choke = next(
        f for f in fns.values() if f["Properties"].get("Handler") == "chokepoint.handler.handler"
    )
    env = choke["Properties"]["Environment"]["Variables"]
    assert env["AGATE_OIDC_JWKS_URL"].endswith("/.well-known/jwks.json")
    assert env["AGATE_OIDC_AUDIENCE"] == "aud123"


def test_assume_user_role_grant_when_auth_role_supplied():
    t = _template({"auth_role_arn": _AUTH_ROLE})
    pols = t.find_resources("AWS::IAM::Policy")
    assumes = [
        s
        for p in pols.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "sts:AssumeRole" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    ]
    assert assumes, "the choke point must be able to assume the user's authenticated role"
    assert any(_AUTH_ROLE in str(s.get("Resource")) for s in assumes)


def test_auth_role_may_invoke_the_function_url():
    # The IAM-authed Function URL needs a resource permission letting the browser's
    # authenticated role invoke it — else the signed POST is 403'd before the handler.
    t = _template({"auth_role_arn": _AUTH_ROLE})
    t.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "Action": "lambda:InvokeFunctionUrl",
            "FunctionUrlAuthType": "AWS_IAM",
            "Principal": _AUTH_ROLE,
        },
    )


def test_auth_role_also_gets_invoke_function_permission():
    # As of Oct 2025 a Function URL requires BOTH lambda:InvokeFunctionUrl AND
    # lambda:InvokeFunction; the latter bounded to URL calls (InvokedViaFunctionUrl).
    t = _template({"auth_role_arn": _AUTH_ROLE})
    t.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "Action": "lambda:InvokeFunction",
            "InvokedViaFunctionUrl": True,
            "Principal": _AUTH_ROLE,
        },
    )


def test_no_invoke_permission_without_auth_role():
    # No auth role supplied -> no invoke permission (nothing to grant to).
    t = _template()
    assert t.find_resources("AWS::Lambda::Permission") == {}


def test_exec_role_can_write_the_scope_spend_debit():
    # The handler records the actual cost against each scope node after an allowed
    # call (dynamodb:UpdateItem on the spend table). Without it the call succeeds at
    # Bedrock but 500s recording the debit.
    t = _template({"spend_table": "agate-spend"})
    pols = t.find_resources("AWS::IAM::Policy")
    updates = [
        s
        for p in pols.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "dynamodb:UpdateItem"
        in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    ]
    assert updates, "the choke point must be able to write the scope-spend debit"
    assert any("agate-spend" in str(s.get("Resource")) for s in updates)


def test_invoker_role_created_only_when_principal_supplied():
    # agate#265: a server-to-server caller (quarry) gets a dedicated assumable role permitting
    # exactly lambda:InvokeFunctionUrl/InvokeFunction on the chokepoint — and nothing else. Opt-in.
    t_off = _template()
    off_names = [
        r["Properties"].get("RoleName") for r in t_off.find_resources("AWS::IAM::Role").values()
    ]
    assert "agate-chokepoint-invoker" not in off_names  # absent by default

    caller = "arn:aws:iam::111122223333:role/quarry-backend"
    t = _template({"invoker_principal_arn": caller})
    roles = t.find_resources("AWS::IAM::Role")
    inv = [
        r for r in roles.values() if r["Properties"].get("RoleName") == "agate-chokepoint-invoker"
    ]
    assert inv, "invoker role must be created when a principal is supplied"
    # It trusts exactly the supplied principal.
    trust = inv[0]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]["AWS"]
    assert caller in str(trust)
    # The invoker's inline policy grants exactly the two lambda invoke actions — no Bedrock, no
    # tables (it never widens data/model access; the handler assumes the USER's role for that).
    invoker_id = next(
        rid
        for rid, r in roles.items()
        if r["Properties"].get("RoleName") == "agate-chokepoint-invoker"
    )
    invoker_actions: list[str] = []
    for p in t.find_resources("AWS::IAM::Policy").values():
        if invoker_id not in str(p["Properties"].get("Roles", "")):
            continue
        for s in p["Properties"]["PolicyDocument"]["Statement"]:
            invoker_actions += s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
    assert "lambda:InvokeFunctionUrl" in invoker_actions
    assert "lambda:InvokeFunction" in invoker_actions
    assert not any(a.startswith(("bedrock:", "dynamodb:")) for a in invoker_actions)
