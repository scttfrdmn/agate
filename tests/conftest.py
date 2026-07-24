"""Test-suite bootstrap.

Several Lambda handler modules construct boto3 clients at IMPORT time (module-level
`_sts = boto3.client(...)`, etc.). Collecting their tests therefore needs a region and
credentials present in the environment — even though no real AWS call is made (the calls are
mocked/stubbed per test). Without this, a machine that has no AWS env (a fresh CI runner or a
contributor who hasn't configured the CLI) fails at COLLECTION with `NoRegionError` /
credential errors, before any test runs.

Set safe dummy values BEFORE any test module is imported. Real AWS is never contacted in unit
tests, so these placeholders only satisfy botocore's client construction. `setdefault` means a
developer's real environment is left untouched.
"""

from __future__ import annotations

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
# Dummy static credentials so botocore's client construction doesn't try to resolve a real
# credential chain at import time. Never used for an actual call.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
