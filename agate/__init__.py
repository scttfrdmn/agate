"""agate — shared pure logic for the agate.

This package holds the two load-bearing, AWS-free pieces (design §3.1):
the claims -> session-tag translation and the tier -> model entitlement table.
Keep it side-effect-free and import-light so it stays unit-testable without AWS
and reusable from both the CDK app and the broker Lambda.
"""

from agate.names import (
    DOCS_BUCKET_PREFIX,
    HANDLE,
    NAME,
    TAG_NAMESPACE,
)

__all__ = ["NAME", "HANDLE", "TAG_NAMESPACE", "DOCS_BUCKET_PREFIX"]
