#!/usr/bin/env python3
"""agate CDK app entry point.

One app, small focused stacks (design §11). Phase 0/1 ships only the identity
stack — the load-bearing crux. Later phases add data/audit/lti/meter/web stacks.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the repo root is importable so `infra`, `agate`, and `policy` resolve
# whether `cdk` invokes us from the root or elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aws_cdk as cdk  # noqa: E402

from infra.stacks.admin import AdminStack  # noqa: E402
from infra.stacks.agent import AgentStack  # noqa: E402
from infra.stacks.audit import AuditStack  # noqa: E402
from infra.stacks.chokepoint import ChokepointStack  # noqa: E402
from infra.stacks.data import DataStack  # noqa: E402
from infra.stacks.demo_idp import DemoIdpStack  # noqa: E402
from infra.stacks.governance import GovernanceStack  # noqa: E402
from infra.stacks.identity import IdentityStack  # noqa: E402
from infra.stacks.lti import LtiStack  # noqa: E402
from infra.stacks.web import WebStack  # noqa: E402

app = cdk.App()

# Account/region come from the standard CDK env (CDK_DEFAULT_*) or context.
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

IdentityStack(app, "agate-identity", env=env)
DataStack(app, "agate-data", env=env)
LtiStack(app, "agate-lti", env=env)
AgentStack(app, "agate-agent", env=env)
AuditStack(app, "agate-audit", env=env)
GovernanceStack(app, "agate-governance", env=env)
WebStack(app, "agate-web", env=env)
# Governed-access console API (Phase 9 Track 1) — admin-gated spend analytics.
AdminStack(app, "agate-admin", env=env)
# Optional Tier 1 — only deploy when an institution requires exact pre-call caps,
# centralized inspection, or non-Bedrock routing (design §2, §12 Phase 6).
ChokepointStack(app, "agate-chokepoint", env=env)
# Demo-only OIDC IdP — a throwaway Cognito User Pool for showing the gateway
# without a campus IdP. Production omits this and points the broker at the real IdP.
DemoIdpStack(app, "agate-demo-idp", env=env)

app.synth()
