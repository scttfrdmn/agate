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
    # One gateway, one MCP target, one Slurm Lambda, the existing Runtime — all present.
    assert len(t.find_resources("AWS::BedrockAgentCore::Gateway")) == 1
    assert len(t.find_resources("AWS::BedrockAgentCore::GatewayTarget")) == 1
    assert len(t.find_resources("AWS::BedrockAgentCore::Runtime")) == 1
    assert len(t.find_resources("AWS::Lambda::Function")) >= 1


def test_gateway_is_mcp_with_custom_jwt_and_tenant_fenced_name(template):
    t, stack = template
    gws = list(t.find_resources("AWS::BedrockAgentCore::Gateway").values())
    props = gws[0]["Properties"]
    assert props["ProtocolType"] == "MCP"
    assert props["AuthorizerType"] == "CUSTOM_JWT"
    # The load-bearing assertion: the gateway name joins the #113 fence `agate-{tenant}-*`.
    assert props["Name"] == stack.gateway_name
    assert props["Name"].startswith("agate-")


def test_slurm_target_declares_both_hpc_tools(template):
    t, _ = template
    tgt = list(t.find_resources("AWS::BedrockAgentCore::GatewayTarget").values())[0]
    payload = str(tgt["Properties"])  # the inline tool schema is nested; names appear in it
    assert "hpc-submit" in payload
    assert "hpc-monitor" in payload


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
    # Default (no oauth/connector context): only the Slurm target, no OAuth provider — absent
    # config produces no connector target (NO CLOCKS; a target is per-request).
    t, _ = template
    assert len(t.find_resources("AWS::BedrockAgentCore::GatewayTarget")) == 1  # slurm only
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
    assert names == ["agate-connector-box", "agate-connector-gdrive", "agate-slurm"]
