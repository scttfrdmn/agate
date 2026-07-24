"""CDK synth assertions for the AgentStack Gateway + Slurm wiring (#136). No deploy.

The FIRST synth test in the repo: it `Template.from_stack`s the AgentStack and asserts the
#136 resources synthesize and the Gateway NAME joins the tenant-fenced ARN family the #113
IAM grant authorizes (`gateway/agate-{tenant}-*`). A mismatch here would mean every compiled
tool grant misses at deploy — so this is the deploy-time analogue of the live IAM proof.

Synth is offline (no AWS creds, no deploy); it exercises the CDK construct graph only.
"""

from __future__ import annotations

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402
from infra.stacks.agent import AgentStack  # noqa: E402

_ENV = cdk.Environment(account="111122223333", region="us-east-1")


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    stack = AgentStack(app, "agate-agent-synth", env=_ENV)
    return assertions.Template.from_stack(stack), stack


def test_gateway_target_oauth_and_lambda_synthesize(template):
    t, _ = template
    # One gateway; two core MCP targets (Slurm + web-fetch #192); the Runtime — all present.
    assert len(t.find_resources("AWS::BedrockAgentCore::Gateway")) == 1
    assert len(t.find_resources("AWS::BedrockAgentCore::GatewayTarget")) == 2
    assert len(t.find_resources("AWS::BedrockAgentCore::Runtime")) == 1
    assert len(t.find_resources("AWS::Lambda::Function")) >= 2  # slurm + webfetch tools


def test_webfetch_tool_wired_with_allowlist(template):
    # The web-fetch tool Lambda (#192) carries the host allowlist env; default = empty
    # (deny-all until an institution configures `-c webfetch_allowlist=`).
    t, _ = template
    fns = t.find_resources("AWS::Lambda::Function")
    webfetch = next(
        f
        for f in fns.values()
        if f["Properties"].get("Handler") == "infra.functions.webfetch.handler.handler"
    )
    assert "AGATE_WEBFETCH_ALLOWLIST" in webfetch["Properties"]["Environment"]["Variables"]


# --- memory hook (#130b, opt-in) --------------------------------------------
_MEM_ARN = "arn:aws:lambda:us-east-1:111122223333:function:agate-memory-tool"


def test_memory_hook_absent_without_context():
    # The default deploy (no memory_tool_arn context) wires NO memory env / grant — the
    # billable opt-in stays off and the Runtime is unchanged.
    app = cdk.App()
    t = assertions.Template.from_stack(AgentStack(app, "agate-agent-nomem", env=_ENV))
    runtimes = list(t.find_resources("AWS::BedrockAgentCore::Runtime").values())
    env = runtimes[0]["Properties"]["EnvironmentVariables"]
    assert "AGATE_MEMORY_TOOL_ARN" not in env
    # no InvokeMemoryTool statement anywhere
    pols = t.find_resources("AWS::IAM::Policy")
    sids = [
        s.get("Sid") for p in pols.values() for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    assert "InvokeMemoryTool" not in sids


def test_memory_hook_wired_when_context_supplied():
    # With the deployed memory tool ARN in context, the Runtime gets the env var and the
    # execution role gets lambda:InvokeFunction scoped to exactly that function.
    app = cdk.App(context={"memory_tool_arn": _MEM_ARN})
    t = assertions.Template.from_stack(AgentStack(app, "agate-agent-mem", env=_ENV))
    runtimes = list(t.find_resources("AWS::BedrockAgentCore::Runtime").values())
    assert runtimes[0]["Properties"]["EnvironmentVariables"]["AGATE_MEMORY_TOOL_ARN"] == _MEM_ARN
    pols = t.find_resources("AWS::IAM::Policy")
    invoke = [
        s
        for p in pols.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if s.get("Sid") == "InvokeMemoryTool"
    ]
    assert len(invoke) == 1
    assert invoke[0]["Action"] == "lambda:InvokeFunction"
    assert invoke[0]["Resource"] == _MEM_ARN


def test_gateway_is_mcp_iam_authed_without_oidc_and_tenant_fenced(template):
    # No OIDC context (the default fixture) -> AWS_IAM authorizer (config-free; the gateway
    # invoke is already IAM-fenced by the #113 grant). Setting CUSTOM_JWT without a config is
    # what failed the first live deploy, so the type must be conditional.
    t, stack = template
    gws = list(t.find_resources("AWS::BedrockAgentCore::Gateway").values())
    props = gws[0]["Properties"]
    assert props["ProtocolType"] == "MCP"
    assert props["AuthorizerType"] == "AWS_IAM"
    assert "AuthorizerConfiguration" not in props  # AWS_IAM needs none
    # The load-bearing assertion: the gateway name joins the #113 fence `agate-{tenant}-*`.
    assert props["Name"] == stack.gateway_name
    assert props["Name"].startswith("agate-")


def test_gateway_uses_custom_jwt_when_oidc_supplied():
    # With a Cognito discovery URL in context, the gateway is CUSTOM_JWT WITH its config
    # (AWS requires the config when that type is set — the two are bound together).
    app = cdk.App(
        context={
            "cognito_discovery_url": (
                "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_x/.well-known/"
                "openid-configuration"
            ),
            "cognito_audience": "client-id",
        }
    )
    stack = AgentStack(app, "agate-agent-jwt", env=_ENV)
    t = assertions.Template.from_stack(stack)
    props = list(t.find_resources("AWS::BedrockAgentCore::Gateway").values())[0]["Properties"]
    assert props["AuthorizerType"] == "CUSTOM_JWT"
    assert "AuthorizerConfiguration" in props


def test_slurm_target_declares_both_hpc_tools(template):
    t, _ = template
    tgt = list(t.find_resources("AWS::BedrockAgentCore::GatewayTarget").values())[0]
    payload = str(tgt["Properties"])  # the inline tool schema is nested; names appear in it
    assert "hpc-submit" in payload
    assert "hpc-monitor" in payload


def test_gateway_execution_role_can_invoke_the_slurm_lambda(template):
    # An MCP-Lambda target (GATEWAY_IAM_ROLE) is invoked AS the gateway's execution role, so
    # that role must hold lambda:InvokeFunction on the Slurm fn — AgentCore validates this at
    # target-create time (the bug that failed the second live deploy).
    t, _ = template
    t.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": assertions.Match.object_like(
                {
                    "Statement": assertions.Match.array_with(
                        [assertions.Match.object_like({"Action": "lambda:InvokeFunction"})]
                    )
                }
            )
        },
    )


def test_slurm_lambda_reads_spend_and_budget_tables(template):
    t, _ = template
    # The Slurm tool's role must be able to read the spend + budget tables (the gate's input).
    t.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": assertions.Match.object_like(
                {
                    "Statement": assertions.Match.array_with(
                        [
                            assertions.Match.object_like(
                                {
                                    "Action": assertions.Match.array_with(["dynamodb:Query"]),
                                }
                            )
                        ]
                    )
                }
            )
        },
    )


# --- #137 workload identity + #133 connector targets (deploy follow-ups) ----


def test_workload_identity_synthesizes_tenant_named(template):
    # The #137 deploy binding: a per-tenant workload-identity directory entry.
    t, stack = template
    wis = list(t.find_resources("AWS::BedrockAgentCore::WorkloadIdentity").values())
    assert len(wis) == 1
    assert wis[0]["Properties"]["Name"] == stack.gateway_name  # agate-{tenant}


def test_no_connector_targets_or_oauth_without_deploy_config(template):
    # Default (no oauth/connector context): the two core targets (Slurm + web-fetch), no OAuth
    # provider — absent config produces no CONNECTOR target (NO CLOCKS; a target is per-request).
    t, _ = template
    assert len(t.find_resources("AWS::BedrockAgentCore::GatewayTarget")) == 2  # slurm + webfetch
    assert len(t.find_resources("AWS::BedrockAgentCore::OAuth2CredentialProvider")) == 0


def test_connector_targets_wired_to_oauth_when_configured():
    # With the OAuth provider + per-connector OpenAPI schemas supplied at deploy, each
    # user-oauth connector becomes an OpenAPI Gateway target attached to the OAuth provider.
    app = cdk.App(
        context={
            "google_oauth_client_id": "cid",
            "google_oauth_secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:x",
            "connector_openapi_gdrive": '{"openapi": "3.0.0"}',
            "connector_openapi_box": '{"openapi": "3.0.0"}',
        }
    )
    stack = AgentStack(app, "agate-agent-conn", env=_ENV)
    t = assertions.Template.from_stack(stack)
    assert len(t.find_resources("AWS::BedrockAgentCore::OAuth2CredentialProvider")) == 1
    names = sorted(
        v["Properties"]["Name"]
        for v in t.find_resources("AWS::BedrockAgentCore::GatewayTarget").values()
    )
    assert names == [
        "agate-connector-box",
        "agate-connector-gdrive",
        "agate-slurm",
        "agate-webfetch",
    ]
