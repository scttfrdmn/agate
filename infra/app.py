#!/usr/bin/env python3
"""agg CDK app entry point.

One app, small focused stacks (design §11). Phase 0/1 ships only the identity
stack — the load-bearing crux. Later phases add data/audit/lti/meter/web stacks.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the repo root is importable so `infra`, `agg`, and `policy` resolve
# whether `cdk` invokes us from the root or elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aws_cdk as cdk  # noqa: E402

from infra.stacks.agent import AgentStack  # noqa: E402
from infra.stacks.audit import AuditStack  # noqa: E402
from infra.stacks.data import DataStack  # noqa: E402
from infra.stacks.identity import IdentityStack  # noqa: E402
from infra.stacks.lti import LtiStack  # noqa: E402

app = cdk.App()

# Account/region come from the standard CDK env (CDK_DEFAULT_*) or context.
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

IdentityStack(app, "agg-identity", env=env)
DataStack(app, "agg-data", env=env)
LtiStack(app, "agg-lti", env=env)
AgentStack(app, "agg-agent", env=env)
AuditStack(app, "agg-audit", env=env)

app.synth()
